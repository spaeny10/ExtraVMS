package tunnel

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/fakehub"
	"axiomvision.ai/bridge/internal/policy"
	"axiomvision.ai/bridge/internal/proto"
)

// marker is put in stream payloads; it must never show up in a log line.
const marker = "PAYLOAD-MARKER-7f3a"

// memTokens is a TokenStore in memory.
type memTokens struct {
	mu  sync.Mutex
	tok string
}

func (m *memTokens) Load() (string, error) { m.mu.Lock(); defer m.mu.Unlock(); return m.tok, nil }
func (m *memTokens) Save(t string) error   { m.mu.Lock(); defer m.mu.Unlock(); m.tok = t; return nil }
func (m *memTokens) Remove() error         { m.mu.Lock(); defer m.mu.Unlock(); m.tok = ""; return nil }

// syncBuf collects log output from several goroutines.
type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) { s.mu.Lock(); defer s.mu.Unlock(); return s.b.Write(p) }
func (s *syncBuf) String() string              { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }

// fakeClock: Sleep hands each wait to the test (which decides when time has passed) and moves Now forward.
type fakeClock struct {
	mu    sync.Mutex
	now   time.Time
	slept chan time.Duration
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC), slept: make(chan time.Duration)}
}

func (c *fakeClock) Now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.now }

func (c *fakeClock) Advance(d time.Duration) { c.mu.Lock(); c.now = c.now.Add(d); c.mu.Unlock() }

func (c *fakeClock) Sleep(ctx context.Context, d time.Duration) error {
	select {
	case c.slept <- d:
		c.Advance(d)
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// nextSleep is the bridge's next reconnect wait.
func (c *fakeClock) nextSleep(t *testing.T) time.Duration {
	t.Helper()
	select {
	case d := <-c.slept:
		return d
	case <-time.After(5 * time.Second):
		t.Fatal("the bridge did not wait to reconnect")
		return 0
	}
}

func loopback(ap netip.AddrPort) bool { return ap.Addr().IsLoopback() }

type bridge struct {
	r      *Runner
	tokens *memTokens
	logs   *syncBuf
	stop   func() time.Duration // cancels Run and returns how long it took to return
}

type option func(*Runner)

// startBridge runs a bridge against the fake hub with token "tok1", loopback exempt from the address rules (so the
// tests' cameras can live on 127.0.0.1) and fast reconnects.
func startBridge(t *testing.T, hub *fakehub.Hub, opts ...option) *bridge {
	t.Helper()
	hub.AddToken("tok1", "br_test")
	logs := &syncBuf{}
	tokens := &memTokens{tok: "tok1"}
	r := &Runner{
		Config:      config.Config{HubURL: hub.URL},
		Tokens:      tokens,
		Policy:      policy.New(policy.Options{Exempt: loopback}),
		Log:         slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
		Version:     "test",
		BackoffBase: 10 * time.Millisecond,
		BackoffMax:  50 * time.Millisecond,
		AuthRetry:   50 * time.Millisecond,
	}
	for _, o := range opts {
		o(r)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = r.Run(ctx)
		close(done)
	}()
	var once sync.Once
	var took time.Duration
	stop := func() time.Duration {
		once.Do(func() {
			start := time.Now()
			cancel()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				t.Error("Run did not return after cancel")
			}
			took = time.Since(start)
		})
		return took
	}
	t.Cleanup(func() {
		stop()
		out := logs.String()
		for _, secret := range []string{"tok1", "tok2", marker} {
			if strings.Contains(out, secret) {
				t.Errorf("log contains %q:\n%s", secret, out)
			}
		}
		if t.Failed() {
			t.Logf("bridge log:\n%s", out)
		}
	})
	return &bridge{r: r, tokens: tokens, logs: logs, stop: stop}
}

// camera is a TCP server on 127.0.0.1 whose accepted connections the test drives.
type camera struct {
	ln    net.Listener
	conns chan net.Conn
	port  int
}

func newCamera(t *testing.T) *camera {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	c := &camera{ln: ln, conns: make(chan net.Conn, 16), port: ln.Addr().(*net.TCPAddr).Port}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			c.conns <- conn
		}
	}()
	t.Cleanup(func() {
		_ = ln.Close()
		for {
			select {
			case conn := <-c.conns:
				_ = conn.Close()
			default:
				return
			}
		}
	})
	return c
}

func (c *camera) accept(t *testing.T) net.Conn {
	t.Helper()
	select {
	case conn := <-c.conns:
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	case <-time.After(5 * time.Second):
		t.Fatal("the bridge did not connect to the camera")
		return nil
	}
}

// echo serves every connection as an echo, half-closing its side once the bridge half-closes.
func echo(t *testing.T) int {
	t.Helper()
	cam := newCamera(t)
	go func() {
		for conn := range cam.conns {
			go func(c net.Conn) {
				_, _ = io.Copy(c, c)
				_ = c.(*net.TCPConn).CloseWrite()
			}(conn)
		}
	}()
	return cam.port
}

func target(id string, port int) proto.Target {
	return proto.Target{ID: id, Host: "127.0.0.1", Ports: []int{port}}
}

func open(id uint32, tgt string, port int) map[string]any {
	return map[string]any{"t": "open", "id": id, "target": tgt, "host": "127.0.0.1", "port": port}
}

// readBody collects stream id's chunks until its `end`, crediting each chunk; it fails on abort.
func readBody(t *testing.T, c *fakehub.Conn, id uint32, credit bool) []byte {
	t.Helper()
	var out []byte
	for {
		m, ok := c.Recv(t, 5*time.Second)
		if !ok {
			t.Fatal("bridge disconnected")
		}
		switch {
		case m.Binary && m.ID == id:
			if len(m.Data) > proto.Chunk {
				t.Fatalf("chunk of %d bytes", len(m.Data))
			}
			out = append(out, m.Data...)
			if credit {
				c.Send(t, proto.Credit{T: "credit", ID: id, Bytes: int64(len(m.Data))})
			}
		case m.T == "end" && uint32(m.Num("id")) == id:
			return out
		case m.T == "abort" && uint32(m.Num("id")) == id:
			t.Fatalf("stream aborted: %s", m.Raw)
		}
	}
}

// isReset: a read error that is a reset, not a clean EOF.
func isReset(err error) bool { return err != nil && !errors.Is(err, io.EOF) }
