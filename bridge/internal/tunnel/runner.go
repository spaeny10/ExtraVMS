package tunnel

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/policy"
	"axiomvision.ai/bridge/internal/proto"
	"axiomvision.ai/bridge/internal/relay"
)

// TokenStore is where the device token lives (config.TokenFile; tests may use memory).
type TokenStore interface {
	Load() (string, error)
	Save(token string) error
	Remove() error
}

// Runner keeps the bridge connected to the hub until its context ends.
type Runner struct {
	Config  config.Config
	Tokens  TokenStore
	Policy  *policy.Policy
	Log     *slog.Logger
	Version string

	StatusPath string       // status.json; "" writes none
	HTTPClient *http.Client // nil: HTTPClient(Config)
	Clock      Clock        // nil: real time
	Rand       func() float64
	// CameraDial replaces the relay's TCP dialer (tests).
	CameraDial func(ctx context.Context, network, addr string) (net.Conn, error)

	PingInterval     time.Duration // WebSocket ping period (20 s)
	DeadAfter        time.Duration // no frame or pong for this long = the hub is gone (40 s)
	HandshakeTimeout time.Duration // dial + TLS + upgrade (15 s)
	AuthRetry        time.Duration // wait after 401/403, or with no token (5 min)
	BackoffBase      time.Duration // first reconnect wait ceiling (1 s)
	BackoffMax       time.Duration // largest reconnect wait (60 s)
	ResetAfter       time.Duration // a connection up this long resets the backoff (60 s)

	Stats relay.Stats

	once    sync.Once
	started time.Time
	stMu    sync.Mutex
	st      config.Status
	stErr   bool
}

func (r *Runner) defaults() {
	r.once.Do(func() {
		if r.Clock == nil {
			r.Clock = realClock{}
		}
		set := func(d *time.Duration, v time.Duration) {
			if *d <= 0 {
				*d = v
			}
		}
		set(&r.PingInterval, 20*time.Second)
		set(&r.DeadAfter, 40*time.Second)
		set(&r.HandshakeTimeout, 15*time.Second)
		set(&r.AuthRetry, 5*time.Minute)
		set(&r.BackoffBase, time.Second)
		set(&r.BackoffMax, 60*time.Second)
		set(&r.ResetAfter, 60*time.Second)
		if r.Log == nil {
			r.Log = slog.New(slog.DiscardHandler)
		}
		if r.Policy == nil {
			r.Policy = policy.New(policy.Options{})
		}
		r.started = time.Now()
		r.st = config.Status{Version: r.Version, PID: os.Getpid(), Hub: r.Config.HubURL, State: "starting"}
	})
}

func (r *Runner) client() (*http.Client, error) {
	if r.HTTPClient != nil {
		return r.HTTPClient, nil
	}
	c, err := HTTPClient(r.Config)
	if err != nil {
		return nil, err
	}
	r.HTTPClient = c
	return c, nil
}

// errRevoked ends a session after the hub's `revoked`.
var errRevoked = errors.New("bridge revoked by the hub")

// errNotEnrolled: there is no token yet.
var errNotEnrolled = errors.New("not enrolled: run axiom-bridge install --code AXB-…")

// Run connects, and reconnects, until ctx ends: full-jitter backoff 1 → 60 s (reset once a connection lasted 60 s),
// AuthRetry after a 401/403 or without a token.
func (r *Runner) Run(ctx context.Context) error {
	r.defaults()
	bo := Backoff{Base: r.BackoffBase, Max: r.BackoffMax, Rand: r.Rand}
	r.Log.Info("bridge starting", "version", r.Version, "hub", r.Config.HubURL)
	for ctx.Err() == nil {
		up, err := r.connectOnce(ctx)
		if ctx.Err() != nil {
			break
		}
		var wait time.Duration
		switch {
		case errors.Is(err, errNotEnrolled):
			wait = r.AuthRetry
			r.setState("not_enrolled", err)
		case IsAuth(err):
			wait = r.AuthRetry
			r.Log.Warn("hub refused the device token; retrying later", "error", err, "retry_in", wait)
		case errors.Is(err, errRevoked):
			wait = r.AuthRetry
		default:
			if up >= r.ResetAfter {
				bo.Reset()
			}
			wait = bo.Next()
			r.Log.Warn("hub connection ended", "error", err, "was_up", up.Round(time.Second), "retry_in", wait.Round(time.Millisecond))
		}
		if !errors.Is(err, errNotEnrolled) && !errors.Is(err, errRevoked) {
			r.setState("waiting", err)
		}
		r.update(func(s *config.Status) { s.NextRetry = r.Clock.Now().Add(wait) })
		if r.Clock.Sleep(ctx, wait) != nil {
			break
		}
	}
	r.setState("stopped", nil)
	r.Log.Info("bridge stopped")
	return nil
}

// connectOnce runs one connection to its end and reports how long it was up (0 if the handshake failed).
func (r *Runner) connectOnce(ctx context.Context) (time.Duration, error) {
	tok, err := r.Tokens.Load()
	if err != nil {
		return 0, err
	}
	if tok == "" {
		return 0, errNotEnrolled
	}
	client, err := r.client()
	if err != nil {
		return 0, err
	}
	r.setState("connecting", nil)
	s := newSession(r)
	hctx, cancel := context.WithTimeout(ctx, r.HandshakeTimeout)
	ws, err := Dial(hctx, client, r.Config.HubURL, "Bearer "+tok, r.Version, s.alive)
	cancel()
	if err != nil {
		return 0, err
	}
	start := r.Clock.Now()
	err = s.run(ctx, ws)
	return r.Clock.Now().Sub(start), err
}

// Verify connects once with the stored token and returns the hub's welcome (install prints the bridge's name and
// Site from it), then closes cleanly.
func (r *Runner) Verify(ctx context.Context) (proto.Welcome, error) {
	r.defaults()
	tok, err := r.Tokens.Load()
	if err != nil || tok == "" {
		return proto.Welcome{}, errors.Join(errNotEnrolled, err)
	}
	client, err := r.client()
	if err != nil {
		return proto.Welcome{}, err
	}
	hctx, cancel := context.WithTimeout(ctx, r.HandshakeTimeout)
	defer cancel()
	ws, err := Dial(hctx, client, r.Config.HubURL, "Bearer "+tok, r.Version, nil)
	if err != nil {
		return proto.Welcome{}, err
	}
	defer ws.CloseNow()
	return awaitWelcome(hctx, ws, Hello(r.Version))
}

// ---- status file

func (r *Runner) update(f func(*config.Status)) {
	r.stMu.Lock()
	f(&r.st)
	r.st.UpdatedAt = time.Now().UTC()
	st := r.st
	st.TargetErrors = r.Stats.TargetErrors()
	st.BytesUp, st.BytesDown = r.Stats.BytesUp.Load(), r.Stats.BytesDown.Load()
	path := r.StatusPath
	r.stMu.Unlock()
	if path == "" {
		return
	}
	if err := config.WriteStatus(path, st); err != nil {
		r.stMu.Lock()
		first := !r.stErr
		r.stErr = true
		r.stMu.Unlock()
		if first {
			r.Log.Warn("cannot write the status file", "path", path, "error", err)
		}
	}
}

func (r *Runner) setState(state string, err error) {
	r.update(func(s *config.Status) {
		if s.State != state {
			s.Since = time.Now().UTC()
		}
		s.State = state
		s.Connected = state == "connected"
		if state != "connected" {
			s.Streams = 0
		}
		if err != nil {
			s.LastError, s.LastErrorAt = err.Error(), time.Now().UTC()
		}
		if state == "connected" {
			s.NextRetry = time.Time{}
		}
	})
}

// Status is a copy of what the status file holds.
func (r *Runner) Status() config.Status {
	r.stMu.Lock()
	defer r.stMu.Unlock()
	return r.st
}
