// Package relay carries the hub's streams to camera TCP ports. Each `open` that passes the policy becomes one TCP
// connection with two pumps: camera → hub (reads only while the hub has granted window, so a slow hub stops the
// camera rather than growing memory) and hub → camera (writes chunks in order and returns credit only after the
// bytes are in the TCP socket). `end` maps to a TCP half-close, `abort` to a reset.
package relay

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"axiomvision.ai/bridge/internal/policy"
	"axiomvision.ai/bridge/internal/proto"
)

// Outbox is the tunnel's single writer as the relay sees it.
type Outbox interface {
	// Ordered queues a control frame behind everything already queued (opened, end, abort, probe_result), so an
	// `end` or `abort` never overtakes the stream's last chunk. It never blocks.
	Ordered(frame any)
	// Data queues a binary chunk in the same order. It blocks while the queue is over its memory cap.
	Data(ctx context.Context, frame []byte) error
	// Priority queues a frame that may jump ahead of queued data (credit). It never blocks.
	Priority(frame any)
}

// Stats are counters that outlive one tunnel connection (heartbeat bytes_up/bytes_down, target_errors).
type Stats struct {
	BytesUp   atomic.Int64 // camera → hub
	BytesDown atomic.Int64 // hub → camera

	mu     sync.Mutex
	errors map[string]string
}

// SetTargetError records the last connect failure for "target:port"; ok clears it.
func (s *Stats) SetTargetError(key, msg string, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if ok {
		delete(s.errors, key)
		return
	}
	if s.errors == nil {
		s.errors = map[string]string{}
	}
	if len(s.errors) < 256 || s.errors[key] != "" { // bounded: the hub assigns few targets
		s.errors[key] = msg
	}
}

// TargetErrors is a copy for the heartbeat (never nil, so it encodes as {}).
func (s *Stats) TargetErrors() map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]string, len(s.errors))
	for k, v := range s.errors {
		out[k] = v
	}
	return out
}

// Relay is the set of streams on one tunnel connection.
type Relay struct {
	out   Outbox
	pol   *policy.Policy
	stats *Stats
	log   *slog.Logger

	// Dial connects to a camera; tests may replace it. DialTimeout bounds each connect.
	Dial        func(ctx context.Context, network, addr string) (net.Conn, error)
	DialTimeout time.Duration

	ctx    context.Context
	cancel context.CancelFunc
	probes chan struct{} // concurrent probes allowed
	wg     sync.WaitGroup

	mu         sync.Mutex
	streams    map[uint32]*stream
	maxStreams int
	window     int64
	closed     bool
}

const (
	defaultDialTimeout = 5 * time.Second
	maxProbes          = 4
	maxWindow          = 1 << 30 // a credit that would grow the window past this is clamped
)

// New returns a relay for one connection, with the default limits until Configure applies the welcome's.
func New(out Outbox, pol *policy.Policy, stats *Stats, log *slog.Logger) *Relay {
	d := &net.Dialer{KeepAlive: 15 * time.Second}
	ctx, cancel := context.WithCancel(context.Background())
	return &Relay{
		out: out, pol: pol, stats: stats, log: log,
		Dial: d.DialContext, DialTimeout: defaultDialTimeout,
		ctx: ctx, cancel: cancel, probes: make(chan struct{}, maxProbes),
		streams: map[uint32]*stream{}, maxStreams: proto.DefaultMaxStreams, window: proto.DefaultWindow,
	}
}

// Configure applies the welcome's stream cap and per-stream window (zero or negative values keep the defaults).
// The window applies to streams opened afterwards.
func (r *Relay) Configure(maxStreams int, window int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if maxStreams > 0 {
		r.maxStreams = maxStreams
	}
	if window > 0 {
		r.window = min(window, maxWindow)
	}
}

// Count is the number of open streams.
func (r *Relay) Count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.streams)
}

func (r *Relay) get(id uint32) *stream {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.streams[id]
}

func (r *Relay) unregister(s *stream) {
	r.mu.Lock()
	if r.streams[s.id] == s {
		delete(r.streams, s.id)
	}
	r.mu.Unlock()
}

func (r *Relay) refuse(id uint32, reason string) {
	r.out.Ordered(proto.Abort{T: "abort", ID: id, Reason: reason})
}

// Open handles the hub's `open`: checks the id, the stream cap and the policy, then dials in the background (the
// reader must never wait on a camera). Hub data that arrives during the dial waits in the stream's inbox.
func (r *Relay) Open(f proto.Open) {
	if f.ID%2 == 0 {
		r.refuse(f.ID, "stream ids opened by the hub must be odd")
		return
	}
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return
	}
	if old := r.streams[f.ID]; old != nil {
		r.mu.Unlock()
		old.kill() // the hub has lost track of it: neither copy can be trusted
		r.refuse(f.ID, "stream id already in use")
		return
	}
	if len(r.streams) >= r.maxStreams {
		n := r.maxStreams
		r.mu.Unlock()
		r.refuse(f.ID, fmt.Sprintf("too many streams (max %d)", n))
		return
	}
	ap, err := r.pol.Check(f.Target, f.Host, f.Port)
	if err != nil {
		r.mu.Unlock()
		r.log.Warn("open refused", "id", f.ID, "target", f.Target, "host", f.Host, "port", f.Port, "reason", err)
		r.refuse(f.ID, err.Error())
		return
	}
	s := newStream(r, f.ID, f.Target, ap, r.window)
	r.streams[f.ID] = s
	r.wg.Add(1)
	r.mu.Unlock()
	go s.run()
}

// Data delivers a hub chunk to its stream; chunks for unknown (finished, aborted) streams are dropped.
func (r *Relay) Data(id uint32, b []byte) {
	if s := r.get(id); s != nil {
		s.deliver(b)
	}
}

// End is the hub's half-close: after the queued chunks are written, the camera socket is shut for writing.
func (r *Relay) End(id uint32) {
	if s := r.get(id); s != nil {
		s.in.pushEnd()
	}
}

// Abort is the hub's reset: the camera connection is closed with RST and nothing more is sent for the stream.
func (r *Relay) Abort(id uint32, reason string) {
	if s := r.get(id); s != nil {
		r.log.Debug("stream aborted by hub", "id", id, "reason", reason)
		s.kill()
	}
}

// Credit returns window to a stream's camera → hub pump.
func (r *Relay) Credit(id uint32, n int64) {
	if n <= 0 {
		return
	}
	if s := r.get(id); s != nil {
		s.win.grant(n)
	}
}

// Recheck aborts every stream the current targets no longer allow (after a `targets` replaced the list).
func (r *Relay) Recheck() {
	r.mu.Lock()
	var gone []*stream
	for _, s := range r.streams {
		if !r.pol.Allowed(s.target, s.ap) {
			gone = append(gone, s)
		}
	}
	r.mu.Unlock()
	for _, s := range gone {
		r.log.Info("stream closed: target no longer assigned", "id", s.id, "target", s.target, "addr", s.ap)
		s.fail("target no longer assigned")
	}
}

// Probe handles the hub's `probe`: a TCP connect test, answered with probe_result.
func (r *Relay) Probe(f proto.Probe) {
	id := f.ID
	if len(id) == 0 {
		id = json.RawMessage("null")
	}
	select {
	case r.probes <- struct{}{}:
	default:
		r.out.Ordered(proto.ProbeResult{T: "probe_result", ID: id, Error: ptr("busy: too many probes at once")})
		return
	}
	r.wg.Add(1)
	go func() {
		defer func() { <-r.probes; r.wg.Done() }()
		start := time.Now()
		res := proto.ProbeResult{T: "probe_result", ID: id}
		ap, err := r.pol.Check(f.Target, f.Host, f.Port)
		if err == nil {
			ctx, cancel := context.WithTimeout(r.ctx, r.DialTimeout)
			var c net.Conn
			c, err = r.Dial(ctx, "tcp", ap.String())
			cancel()
			key := targetKey(f.Target, ap.Port())
			if err == nil {
				_ = c.Close()
				r.stats.SetTargetError(key, "", true)
			} else {
				err = errors.New(DialError(err))
				r.stats.SetTargetError(key, err.Error(), false)
			}
		}
		res.MS = time.Since(start).Milliseconds()
		res.OK = err == nil
		if err != nil {
			res.Error = ptr(err.Error())
		}
		r.log.Info("probe", "target", f.Target, "host", f.Host, "port", f.Port, "ok", res.OK, "ms", res.MS, "error", err)
		r.out.Ordered(res)
	}()
}

// Close ends every stream with a reset (the tunnel is gone, so nothing is sent) and waits for their goroutines.
func (r *Relay) Close() {
	r.mu.Lock()
	r.closed = true
	all := make([]*stream, 0, len(r.streams))
	for _, s := range r.streams {
		all = append(all, s)
	}
	r.mu.Unlock()
	r.cancel()
	for _, s := range all {
		s.kill()
	}
	r.wg.Wait()
}

func ptr(s string) *string { return &s }

func targetKey(target string, port uint16) string { return fmt.Sprintf("%s:%d", target, port) }

// DialError shortens a connect error to what an admin needs on the hub ("connection refused", "timeout").
func DialError(err error) string {
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
		return "timeout"
	}
	s := strings.ToLower(err.Error())
	switch {
	case strings.Contains(s, "refused"):
		return "connection refused"
	case strings.Contains(s, "network is unreachable") || strings.Contains(s, "unreachable network"):
		return "network unreachable"
	case strings.Contains(s, "unreachable") || strings.Contains(s, "no route"):
		return "host unreachable"
	case strings.Contains(s, "reset"):
		return "connection reset"
	}
	msg := err.Error()
	if i := strings.LastIndex(msg, ": "); i >= 0 {
		msg = msg[i+2:]
	}
	return msg
}
