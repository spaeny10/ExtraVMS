package relay

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"

	"axiomvision.ai/bridge/internal/proto"
)

// stream is one hub-opened TCP connection to a camera port.
type stream struct {
	r      *Relay
	id     uint32
	target string
	ap     netip.AddrPort
	key    string // "target:port" for heartbeat target_errors

	win *window // bytes we may still send to the hub
	in  *inbox  // hub → camera bytes not yet written

	ctx    context.Context // cancelled when the stream dies
	cancel context.CancelFunc
	dead   atomic.Bool // aborted (by either side) or finished: send nothing more

	mu   sync.Mutex
	conn net.Conn // nil until the dial succeeds
}

func newStream(r *Relay, id uint32, target string, ap netip.AddrPort, window int64) *stream {
	ctx, cancel := context.WithCancel(r.ctx)
	return &stream{
		r: r, id: id, target: target, ap: ap, key: targetKey(target, ap.Port()),
		win: newWindow(window), in: newInbox(window),
		ctx: ctx, cancel: cancel,
	}
}

// run dials the camera, answers `opened`, then runs both pumps until both directions have ended or one side aborts.
func (s *stream) run() {
	defer s.r.wg.Done()
	dctx, cancel := context.WithTimeout(s.ctx, s.r.DialTimeout)
	c, err := s.r.Dial(dctx, "tcp", s.ap.String())
	cancel()
	if err != nil {
		if s.ctx.Err() != nil { // aborted by the hub (or the tunnel went away) while dialling
			return
		}
		msg := DialError(err)
		s.r.stats.SetTargetError(s.key, msg, false)
		s.r.log.Warn("camera connect failed", "id", s.id, "target", s.target, "addr", s.ap, "error", msg)
		s.fail("connect " + s.ap.String() + ": " + msg)
		return
	}
	s.r.stats.SetTargetError(s.key, "", true)
	s.mu.Lock()
	if s.dead.Load() {
		s.mu.Unlock()
		reset(c)
		return
	}
	s.conn = c
	s.mu.Unlock()
	s.r.log.Debug("stream opened", "id", s.id, "target", s.target, "addr", s.ap)
	s.r.out.Ordered(proto.Opened{T: "opened", ID: s.id})

	done := make(chan struct{})
	go func() { s.up(c); close(done) }()
	s.down(c)
	<-done
	s.finish()
}

// up is camera → hub. It reads only as many bytes as the hub's window allows: with no credit it stops reading, and
// TCP flow control then holds the camera back.
func (s *stream) up(c net.Conn) {
	buf := make([]byte, proto.Chunk)
	for {
		avail, err := s.win.wait(s.ctx)
		if err != nil {
			return
		}
		n, err := c.Read(buf[:min(avail, int64(len(buf)))])
		if n > 0 {
			s.win.take(int64(n))
			s.r.stats.BytesUp.Add(int64(n))
			// a copy sized to n: queued frames hold only their own bytes, whatever the read size was
			if s.dead.Load() || s.r.out.Data(s.ctx, proto.ChunkFrame(s.id, buf[:n])) != nil {
				return
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				if !s.dead.Load() {
					s.r.out.Ordered(proto.End{T: "end", ID: s.id}) // behind the last chunk, same queue
				}
				return
			}
			s.fail("camera read: " + DialError(err))
			return
		}
	}
}

// down is hub → camera: each chunk is written to the socket, then credited back to the hub.
func (s *stream) down(c net.Conn) {
	for {
		b, end, err := s.in.pop(s.ctx)
		if err != nil {
			return
		}
		if end {
			if cw, ok := c.(interface{ CloseWrite() error }); ok {
				_ = cw.CloseWrite()
			}
			return
		}
		if _, err := c.Write(b); err != nil {
			s.fail("camera write: " + DialError(err))
			return
		}
		s.r.stats.BytesDown.Add(int64(len(b)))
		if !s.dead.Load() {
			s.r.out.Priority(proto.Credit{T: "credit", ID: s.id, Bytes: int64(len(b))})
		}
	}
}

// deliver queues hub bytes for the camera. A hub that sends past the window it was granted is aborted rather than
// buffered without bound.
func (s *stream) deliver(b []byte) {
	switch s.in.push(b) {
	case pushOK:
	case pushOverWindow:
		s.fail("hub exceeded the stream window")
	case pushAfterEnd:
		s.fail("data after end")
	}
}

// fail is a local abort: tell the hub, reset the camera connection.
func (s *stream) fail(reason string) {
	if !s.dead.CompareAndSwap(false, true) {
		return
	}
	s.r.unregister(s)
	s.r.out.Ordered(proto.Abort{T: "abort", ID: s.id, Reason: reason})
	s.teardown(true)
}

// kill is a remote abort (hub `abort`, duplicate id, tunnel gone): reset the camera connection, send nothing.
func (s *stream) kill() {
	if !s.dead.CompareAndSwap(false, true) {
		return
	}
	s.r.unregister(s)
	s.teardown(true)
}

// finish is the clean end: both directions half-closed, so a plain close.
func (s *stream) finish() {
	if !s.dead.CompareAndSwap(false, true) {
		return
	}
	s.r.unregister(s)
	s.r.log.Debug("stream finished", "id", s.id, "target", s.target, "addr", s.ap)
	s.teardown(false)
}

func (s *stream) teardown(rst bool) {
	s.cancel()
	s.mu.Lock()
	c := s.conn
	s.mu.Unlock()
	if c == nil {
		return
	}
	if rst {
		reset(c)
	} else {
		_ = c.Close()
	}
}

// reset closes with SO_LINGER 0, so the camera sees RST (the meaning of `abort`) rather than a clean FIN.
func reset(c net.Conn) {
	if tc, ok := c.(*net.TCPConn); ok {
		_ = tc.SetLinger(0)
	}
	_ = c.Close()
}

// window is the credit a sender holds: Take before sending, Grant when the receiver credits.
type window struct {
	mu    sync.Mutex
	avail int64
	sig   chan struct{}
}

func newWindow(n int64) *window { return &window{avail: n, sig: make(chan struct{}, 1)} }

// wait blocks until some window is available and returns how much.
func (w *window) wait(ctx context.Context) (int64, error) {
	for {
		w.mu.Lock()
		a := w.avail
		w.mu.Unlock()
		if a > 0 {
			return a, nil
		}
		select {
		case <-w.sig:
		case <-ctx.Done():
			return 0, ctx.Err()
		}
	}
}

// take consumes n bytes; only the stream's one pump takes, so n never exceeds what wait returned.
func (w *window) take(n int64) {
	w.mu.Lock()
	w.avail -= n
	w.mu.Unlock()
}

func (w *window) grant(n int64) {
	w.mu.Lock()
	w.avail = min(w.avail+n, maxWindow)
	w.mu.Unlock()
	select {
	case w.sig <- struct{}{}:
	default:
	}
}

// inbox holds hub → camera chunks until the camera socket takes them, bounded by the stream window.
type inbox struct {
	mu    sync.Mutex
	items [][]byte
	bytes int64
	limit int64
	end   bool
	sig   chan struct{}
}

type pushResult int

const (
	pushOK pushResult = iota
	pushOverWindow
	pushAfterEnd
)

func newInbox(limit int64) *inbox { return &inbox{limit: limit, sig: make(chan struct{}, 1)} }

func (b *inbox) push(p []byte) pushResult {
	if len(p) == 0 {
		return pushOK
	}
	b.mu.Lock()
	switch {
	case b.end:
		b.mu.Unlock()
		return pushAfterEnd
	case b.bytes+int64(len(p)) > b.limit:
		b.mu.Unlock()
		return pushOverWindow
	}
	b.items = append(b.items, p)
	b.bytes += int64(len(p))
	b.mu.Unlock()
	b.wake()
	return pushOK
}

func (b *inbox) pushEnd() {
	b.mu.Lock()
	b.end = true
	b.mu.Unlock()
	b.wake()
}

func (b *inbox) wake() {
	select {
	case b.sig <- struct{}{}:
	default:
	}
}

// pop returns the next chunk, or end once every chunk before the hub's `end` has been taken.
func (b *inbox) pop(ctx context.Context) ([]byte, bool, error) {
	for {
		b.mu.Lock()
		if len(b.items) > 0 {
			p := b.items[0]
			b.items[0] = nil
			b.items = b.items[1:]
			b.bytes -= int64(len(p))
			b.mu.Unlock()
			return p, false, nil
		}
		end := b.end
		b.mu.Unlock()
		if end {
			return nil, true, nil
		}
		select {
		case <-b.sig:
		case <-ctx.Done():
			return nil, false, ctx.Err()
		}
	}
}
