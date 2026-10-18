package tunnel

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/proto"
	"axiomvision.ai/bridge/internal/relay"
)

// session is one live connection: its queue, its relay, and the goroutines around the socket.
type session struct {
	r     *Runner
	q     *queue
	relay *relay.Relay
	last  atomic.Int64       // unix nanoseconds of the last frame or pong from the hub
	hb    chan time.Duration // heartbeat period, sent once the welcome arrives
	ping  atomic.Bool        // a ping is in flight
}

var errHubSilent = errors.New("hub not answering (no frame or pong in time)")

func newSession(r *Runner) *session {
	s := &session{r: r, q: newQueue(), hb: make(chan time.Duration, 1)}
	s.alive()
	return s
}

func (s *session) alive() { s.last.Store(time.Now().UnixNano()) }

// run serves the connection until it fails or ctx ends, then tears it down: camera sockets reset, a close frame
// to the hub (bounded, so a service stop finishes well within 5 s), every goroutine joined.
func (s *session) run(ctx context.Context, ws *websocket.Conn) error {
	r := s.r
	r.Policy.SetTargets(0, nil) // nothing is reachable until this connection's own `targets`
	s.relay = relay.New(s.q, r.Policy, &r.Stats, r.Log)
	if r.CameraDial != nil {
		s.relay.Dial = r.CameraDial
	}
	s.alive()
	s.q.Ordered(Hello(r.Version)) // the first frame out

	sctx, cancel := context.WithCancel(context.Background())
	errc := make(chan error, 4)
	var wg sync.WaitGroup
	for _, f := range []func(context.Context, *websocket.Conn) error{s.writer, s.reader, s.keepalive, s.heartbeat} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errc <- f(sctx, ws)
		}()
	}
	var err error
	code, reason := websocket.StatusNormalClosure, "bye"
	select {
	case err = <-errc:
	case <-ctx.Done():
		err, code, reason = ctx.Err(), websocket.StatusGoingAway, "bridge stopping"
	}
	if errors.Is(err, errRevoked) {
		reason = "revoked"
	}
	s.q.close()
	s.relay.Close()
	closeWS(ws, code, reason, 2*time.Second)
	cancel()
	wg.Wait()
	return err
}

// closeWS sends a close frame and waits a little for the hub's; then drops the socket regardless.
func closeWS(ws *websocket.Conn, code websocket.StatusCode, reason string, wait time.Duration) {
	done := make(chan struct{})
	go func() {
		_ = ws.Close(code, reason)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(wait):
		_ = ws.CloseNow()
		<-done
	}
}

// writer is the only goroutine that writes to the socket.
func (s *session) writer(ctx context.Context, ws *websocket.Conn) error {
	for {
		it, err := s.q.next(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		wctx, cancel := context.WithTimeout(ctx, s.r.DeadAfter)
		err = ws.Write(wctx, it.typ, it.b)
		cancel()
		if err != nil {
			return fmt.Errorf("write: %w", err)
		}
	}
}

// reader dispatches the hub's frames. It never blocks on a camera or on the queue.
func (s *session) reader(ctx context.Context, ws *websocket.Conn) error {
	for {
		typ, rd, err := ws.Reader(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if cs := websocket.CloseStatus(err); cs != -1 {
				var ce websocket.CloseError
				errors.As(err, &ce)
				return fmt.Errorf("hub closed the connection (%d %s)", cs, ce.Reason)
			}
			return fmt.Errorf("read: %w", err)
		}
		limit := int64(proto.MaxText)
		if typ == websocket.MessageBinary {
			limit = proto.MaxBinary
		}
		b, err := io.ReadAll(io.LimitReader(rd, limit+1))
		if err != nil {
			return fmt.Errorf("read: %w", err)
		}
		if int64(len(b)) > limit {
			return fmt.Errorf("hub sent a %s frame over %d bytes", typ, limit)
		}
		s.alive()
		if typ == websocket.MessageBinary {
			id, data, err := proto.ParseChunk(b)
			if err != nil {
				s.r.Log.Debug("ignored a short binary frame", "bytes", len(b))
				continue
			}
			s.relay.Data(id, data)
			continue
		}
		if err := s.handle(b); err != nil {
			return err
		}
	}
}

// handle is one control frame. Unknown types, unknown fields and frames that do not decode are ignored (logged),
// so a newer hub never breaks an older bridge.
func (s *session) handle(b []byte) error {
	r := s.r
	t, err := proto.TypeOf(b)
	if err != nil {
		r.Log.Warn("ignored a control frame that is not JSON", "bytes", len(b))
		return nil
	}
	bad := func(err error) {
		r.Log.Warn("ignored a malformed frame", "t", t, "error", err)
	}
	switch t {
	case "welcome":
		var w proto.Welcome
		if err := proto.Decode(b, &w); err != nil {
			bad(err)
			return nil
		}
		s.relay.Configure(w.MaxStreams, w.Window)
		hb := w.HeartbeatS
		if hb <= 0 {
			hb = proto.DefaultHeartbeatS
		}
		r.update(func(st *config.Status) { st.BridgeID, st.Name, st.Site = w.BridgeID, w.Name, w.SiteName() })
		r.setState("connected", nil)
		r.Log.Info("connected to the hub", "bridge_id", w.BridgeID, "name", w.Name, "site", w.SiteName(),
			"max_streams", w.MaxStreams, "window", w.Window)
		s.sendHeartbeat()
		select {
		case s.hb <- time.Duration(hb) * time.Second:
		default:
		}
	case "targets":
		var tg proto.Targets
		if err := proto.Decode(b, &tg); err != nil {
			bad(err)
			return nil
		}
		if badIDs := r.Policy.SetTargets(tg.Rev, tg.Items); len(badIDs) > 0 {
			r.Log.Warn("targets without an IP address are ignored", "targets", badIDs)
		}
		s.relay.Recheck()
		r.update(func(st *config.Status) { st.TargetsRev, st.Targets = tg.Rev, len(tg.Items) })
		r.Log.Info("targets updated", "rev", tg.Rev, "count", len(tg.Items))
	case "open":
		var o proto.Open
		if err := proto.Decode(b, &o); err != nil {
			bad(err)
			return nil
		}
		s.relay.Open(o)
	case "probe":
		var p proto.Probe
		if err := proto.Decode(b, &p); err != nil {
			bad(err)
			return nil
		}
		s.relay.Probe(p)
	case "end", "abort", "credit":
		var ref proto.StreamRef
		if err := proto.Decode(b, &ref); err != nil {
			bad(err)
			return nil
		}
		switch t {
		case "end":
			s.relay.End(ref.ID)
		case "abort":
			s.relay.Abort(ref.ID, ref.Reason)
		default:
			s.relay.Credit(ref.ID, ref.Bytes)
		}
	case "ping":
		var p proto.Ping
		_ = proto.Decode(b, &p)
		s.q.Priority(proto.Pong{T: "pong", TS: p.TS})
	case "pong":
	case "rotate":
		var rt proto.Rotate
		if err := proto.Decode(b, &rt); err != nil || rt.Token == "" {
			bad(errors.Join(err, errors.New("no token")))
			return nil
		}
		if err := r.Tokens.Save(rt.Token); err != nil {
			r.Log.Error("could not save the rotated token; keeping the old one", "error", err)
			return nil
		}
		s.q.Ordered(proto.Rotated{T: "rotated"})
		r.Log.Info("device token rotated")
	case "revoked":
		var rv proto.Revoked
		_ = proto.Decode(b, &rv)
		r.Log.Warn("the hub revoked this bridge; re-enroll with a new code", "reason", rv.Reason)
		if err := r.Tokens.Remove(); err != nil {
			r.Log.Error("could not delete the revoked token", "error", err)
		}
		r.setState("revoked", fmt.Errorf("revoked by the hub: %s", rv.Reason))
		return errRevoked
	default:
		r.Log.Debug("ignored an unknown frame type", "t", t)
	}
	return nil
}

// keepalive pings every PingInterval and declares the hub gone after DeadAfter without a frame or a pong.
func (s *session) keepalive(ctx context.Context, ws *websocket.Conn) error {
	pt := time.NewTicker(s.r.PingInterval)
	defer pt.Stop()
	ct := time.NewTicker(max(s.r.DeadAfter/8, 10*time.Millisecond))
	defer ct.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-pt.C:
			if s.ping.CompareAndSwap(false, true) {
				go func() {
					pctx, cancel := context.WithTimeout(ctx, s.r.DeadAfter)
					_ = ws.Ping(pctx) // the pong lands in alive() via OnPongReceived
					cancel()
					s.ping.Store(false)
				}()
			}
		case <-ct.C:
			if time.Since(time.Unix(0, s.last.Load())) > s.r.DeadAfter {
				return errHubSilent
			}
		}
	}
}

// heartbeat starts once the welcome gives its period.
func (s *session) heartbeat(ctx context.Context, _ *websocket.Conn) error {
	var tick <-chan time.Time
	var t *time.Ticker
	defer func() {
		if t != nil {
			t.Stop()
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return nil
		case d := <-s.hb:
			if t != nil {
				t.Stop()
			}
			t = time.NewTicker(d)
			tick = t.C
		case <-tick:
			s.sendHeartbeat()
		}
	}
}

func (s *session) sendHeartbeat() {
	r := s.r
	n := s.relay.Count()
	s.q.Priority(proto.Heartbeat{T: "heartbeat", Streams: n, BytesUp: r.Stats.BytesUp.Load(), BytesDown: r.Stats.BytesDown.Load(),
		UptimeS: int64(time.Since(r.started).Seconds()), TargetErrors: r.Stats.TargetErrors()})
	r.update(func(st *config.Status) { st.Streams = n })
}

// awaitWelcome sends hello on a fresh connection and waits for the welcome (install's check), then closes.
func awaitWelcome(ctx context.Context, ws *websocket.Conn, hello proto.Hello) (proto.Welcome, error) {
	b, _ := proto.Encode(hello)
	if err := ws.Write(ctx, websocket.MessageText, b); err != nil {
		return proto.Welcome{}, err
	}
	for {
		typ, data, err := ws.Read(ctx)
		if err != nil {
			return proto.Welcome{}, fmt.Errorf("waiting for the hub's welcome: %w", err)
		}
		if typ != websocket.MessageText {
			continue
		}
		t, _ := proto.TypeOf(data)
		switch t {
		case "welcome":
			var w proto.Welcome
			if err := proto.Decode(data, &w); err != nil {
				return w, err
			}
			closeWS(ws, websocket.StatusNormalClosure, "verified", time.Second)
			return w, nil
		case "revoked":
			return proto.Welcome{}, errRevoked
		}
	}
}
