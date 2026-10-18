package tunnel

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/fakehub"
	"axiomvision.ai/bridge/internal/policy"
	"axiomvision.ai/bridge/internal/proto"
)

const wait = 5 * time.Second

func TestHelloWelcomeHeartbeat(t *testing.T) {
	hub := fakehub.New(t)
	b := startBridge(t, hub, func(r *Runner) { r.StatusPath = filepath.Join(t.TempDir(), "status.json") })
	c := hub.Accept(t, wait)
	if c.Token != "tok1" {
		t.Fatalf("token %q", c.Token)
	}
	hello := c.Welcome(t, map[string]any{"heartbeat_s": 1})
	if hello.Num("proto") != 1 || hello.Str("version") != "test" || hello.Str("os") == "" || hello.Str("hostname") == "" {
		t.Fatalf("hello %s", hello.Raw)
	}
	if caps, _ := hello.Frame["caps"].([]any); len(caps) != 2 || caps[0] != "tcp" || caps[1] != "probe" {
		t.Fatalf("caps %v", hello.Frame["caps"])
	}
	if _, ok := hello.Frame["lan"].([]any); !ok {
		t.Fatalf("lan %v", hello.Frame["lan"])
	}
	hb := c.Expect(t, "heartbeat", wait) // right after the welcome
	for _, k := range []string{"streams", "bytes_up", "bytes_down", "uptime_s", "target_errors"} {
		if _, ok := hb.Frame[k]; !ok {
			t.Fatalf("heartbeat lacks %s: %s", k, hb.Raw)
		}
	}
	start := time.Now()
	c.Expect(t, "heartbeat", 3*time.Second) // then every heartbeat_s
	if time.Since(start) < 500*time.Millisecond {
		t.Fatal("heartbeat period not applied")
	}
	st, err := config.ReadStatus(b.r.StatusPath)
	if err != nil || !st.Connected || st.Name != "Test bridge" || st.Site != "Test Site" || st.BridgeID != "br_test" {
		t.Fatalf("status %+v %v", st, err)
	}
	if strings.Contains(mustRead(t, b.r.StatusPath), "tok1") {
		t.Fatal("token in the status file")
	}
}

func mustRead(t *testing.T, path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestBackoffFullJitter(t *testing.T) {
	b := Backoff{Base: time.Second, Max: 60 * time.Second, Rand: func() float64 { return 0.999999 }}
	want := []int{1, 2, 4, 8, 16, 32, 60, 60, 60}
	for i, w := range want {
		got := b.Next()
		if got.Round(time.Second) != time.Duration(w)*time.Second {
			t.Fatalf("attempt %d: %s, want ~%ds", i, got, w)
		}
	}
	b.Reset()
	if got := b.Next(); got > time.Second {
		t.Fatalf("after reset %s", got)
	}
	zero := Backoff{Base: time.Second, Max: time.Minute, Rand: func() float64 { return 0 }}
	if zero.Next() != 0 {
		t.Fatal("full jitter starts at 0")
	}
	for range 100 { // never overflows
		zero.Next()
	}
}

func TestReconnectBackoffWithInjectedClock(t *testing.T) {
	hub := fakehub.New(t)
	clk := newFakeClock()
	startBridge(t, hub, func(r *Runner) {
		r.Clock = clk
		r.Rand = func() float64 { return 0.5 }
		r.BackoffBase, r.BackoffMax = time.Second, 60*time.Second
	})
	// the hub drops each connection at once: waits grow 0.5, 1, 2, 4 … capped at 30 (half of 60)
	for _, want := range []time.Duration{500, 1000, 2000, 4000, 8000, 16000, 30000, 30000} {
		c := hub.Accept(t, wait)
		c.Close()
		if got := clk.nextSleep(t); got != want*time.Millisecond {
			t.Fatalf("wait %s, want %s", got, want*time.Millisecond)
		}
	}
	// a connection that stays up 61 s (by the bridge's clock) resets the backoff
	c := hub.Accept(t, wait)
	c.Expect(t, "hello", wait)
	clk.Advance(61 * time.Second)
	c.Close()
	if got := clk.nextSleep(t); got != 500*time.Millisecond {
		t.Fatalf("after a long connection: %s", got)
	}
	// and a short one after that keeps growing
	hub.Accept(t, wait).Close()
	if got := clk.nextSleep(t); got != time.Second {
		t.Fatalf("second wait after reset: %s", got)
	}
}

func TestUnauthorizedWaitsFiveMinutes(t *testing.T) {
	hub := fakehub.New(t)
	clk := newFakeClock()
	b := startBridge(t, hub, func(r *Runner) { r.Clock = clk; r.AuthRetry = 0 }) // 0 = the 5 min default
	hub.RemoveToken("tok1")
	b.tokens.Save("tok-unknown")
	for range 2 {
		if got := clk.nextSleep(t); got != 5*time.Minute {
			t.Fatalf("wait after 401: %s", got)
		}
	}
	st := b.r.Status()
	if (st.State != "waiting" && st.State != "connecting") || !strings.Contains(st.LastError, "401") {
		t.Fatalf("status %+v", st)
	}
	if strings.Contains(b.logs.String(), "tok-unknown") {
		t.Fatal("token logged")
	}
}

func TestSilentHubIsDroppedAndRedialled(t *testing.T) {
	hub := fakehub.New(t)
	hub.NoPong.Store(true)
	startBridge(t, hub, func(r *Runner) { r.PingInterval, r.DeadAfter = 50*time.Millisecond, 300*time.Millisecond })
	c := hub.Accept(t, wait)
	c.Welcome(t, nil)
	start := time.Now()
	c.Gone(t, 3*time.Second) // no frames and no pongs: dead after DeadAfter
	if took := time.Since(start); took < 250*time.Millisecond {
		t.Fatalf("dropped after %s, before DeadAfter", took)
	}
	hub.Accept(t, wait) // and it dials again
}

func TestPongsKeepAQuietHubAlive(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub, func(r *Runner) { r.PingInterval, r.DeadAfter = 50*time.Millisecond, 300*time.Millisecond })
	c := hub.Accept(t, wait)
	c.Welcome(t, nil)
	c.Quiet(t, time.Second) // the hub sends nothing for over 3 × DeadAfter, but answers pings
}

func TestOpenEchoAndEndOrdering(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	port := echo(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_echo", port))
	c.Send(t, open(1, "bt_echo", port))
	if m := c.Expect(t, "opened", wait); m.Num("id") != 1 {
		t.Fatalf("opened %s", m.Raw)
	}
	payload := []byte("hello " + marker)
	c.SendChunk(t, 1, payload)
	c.Send(t, proto.End{T: "end", ID: 1}) // half-close: the echo server then closes its side
	var got []byte
	credited := int64(0)
	for {
		m, _ := c.Recv(t, wait)
		if m.Binary {
			got = append(got, m.Data...)
			continue
		}
		if m.T == "credit" {
			credited += m.Num("bytes")
			continue
		}
		if m.T == "end" {
			break
		}
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("echo %q", got)
	}
	// the credit for our bytes comes once they are in the TCP socket; it may trail the echo
	deadline := time.Now().Add(wait)
	for credited < int64(len(payload)) && time.Now().Before(deadline) {
		if m, _ := c.Recv(t, wait); m.T == "credit" {
			credited += m.Num("bytes")
		}
	}
	if credited != int64(len(payload)) {
		t.Fatalf("credited %d of %d", credited, len(payload))
	}
}

func TestLargeWritesSplitAndEndComesLast(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	cam := newCamera(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_cam", cam.port))
	c.Send(t, open(3, "bt_cam", cam.port))
	conn := cam.accept(t)
	c.Expect(t, "opened", wait)
	body := make([]byte, 200_000)
	for i := range body {
		body[i] = byte(i % 253)
	}
	go func() {
		_, _ = conn.Write(body)
		_ = conn.(*net.TCPConn).CloseWrite()
	}()
	got := readBody(t, c, 3, true) // fails on any chunk > CHUNK; returns at `end`
	if !bytes.Equal(got, body) {
		t.Fatalf("got %d bytes, want %d (end overtook data?)", len(got), len(body))
	}
}

func TestWithheldCreditStopsReading(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	cam := newCamera(t)
	c := hub.Accept(t, wait)
	const window = 100_000
	c.Welcome(t, map[string]any{"window": window}, target("bt_cam", cam.port))
	c.Send(t, open(5, "bt_cam", cam.port))
	conn := cam.accept(t)
	c.Expect(t, "opened", wait)
	var written atomic.Int64
	go func() {
		chunk := make([]byte, 32*1024)
		for {
			n, err := conn.Write(chunk)
			written.Add(int64(n))
			if err != nil {
				return
			}
		}
	}()
	recv := func(n int) {
		t.Helper()
		got := 0
		for got < n {
			m, _ := c.Recv(t, wait)
			if m.Binary {
				got += len(m.Data)
			} else if m.T == "abort" {
				t.Fatalf("aborted: %s", m.Raw)
			}
		}
		if got != n {
			t.Fatalf("received %d, want exactly %d", got, n)
		}
	}
	recv(window)
	c.Quiet(t, 300*time.Millisecond) // no credit: not one more byte
	before := written.Load()
	time.Sleep(200 * time.Millisecond)
	if written.Load() != before {
		t.Fatalf("camera kept writing (%d → %d): the bridge is still reading", before, written.Load())
	}
	c.Send(t, proto.Credit{T: "credit", ID: 5, Bytes: 50_000})
	recv(50_000)
	c.Quiet(t, 300*time.Millisecond)
}

func TestHalfCloseFromHub(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	cam := newCamera(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_cam", cam.port))
	c.Send(t, open(7, "bt_cam", cam.port))
	conn := cam.accept(t)
	c.Expect(t, "opened", wait)
	c.SendChunk(t, 7, []byte("request"))
	c.Send(t, proto.End{T: "end", ID: 7})
	all, err := io.ReadAll(conn) // the camera sees the bytes, then FIN
	if err != nil || string(all) != "request" {
		t.Fatalf("camera read %q %v", all, err)
	}
	// the other direction still works after the hub's end
	_, _ = conn.Write([]byte("response"))
	_ = conn.(*net.TCPConn).CloseWrite()
	if got := readBody(t, c, 7, true); string(got) != "response" {
		t.Fatalf("hub got %q", got)
	}
}

func TestHalfCloseFromCamera(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	cam := newCamera(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_cam", cam.port))
	c.Send(t, open(9, "bt_cam", cam.port))
	conn := cam.accept(t)
	c.Expect(t, "opened", wait)
	_, _ = conn.Write([]byte("banner"))
	_ = conn.(*net.TCPConn).CloseWrite()
	if got := readBody(t, c, 9, true); string(got) != "banner" {
		t.Fatalf("hub got %q", got)
	}
	c.SendChunk(t, 9, []byte("late"))
	c.Send(t, proto.End{T: "end", ID: 9})
	all, err := io.ReadAll(conn)
	if err != nil || string(all) != "late" {
		t.Fatalf("camera read %q %v", all, err)
	}
}

func TestHubAbortResetsCamera(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	cam := newCamera(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_cam", cam.port))
	c.Send(t, open(11, "bt_cam", cam.port))
	conn := cam.accept(t)
	c.Expect(t, "opened", wait)
	c.Send(t, proto.Abort{T: "abort", ID: 11, Reason: "viewer left"})
	_ = conn.SetReadDeadline(time.Now().Add(wait))
	_, err := conn.Read(make([]byte, 10))
	if !isReset(err) {
		t.Fatalf("camera read after abort: %v, want a reset", err)
	}
	c.Quiet(t, 200*time.Millisecond) // the bridge answers nothing to an abort
}

func TestStreamCap(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	port := echo(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, map[string]any{"max_streams": 2}, target("bt_echo", port))
	c.Send(t, open(1, "bt_echo", port))
	c.Expect(t, "opened", wait)
	c.Send(t, open(3, "bt_echo", port))
	c.Expect(t, "opened", wait)
	c.Send(t, open(5, "bt_echo", port))
	m := c.Expect(t, "abort", wait)
	if m.Num("id") != 5 || !strings.Contains(m.Str("reason"), "too many streams") {
		t.Fatalf("abort %s", m.Raw)
	}
	c.Send(t, proto.Abort{T: "abort", ID: 1}) // a slot frees up
	time.Sleep(100 * time.Millisecond)
	c.Send(t, open(7, "bt_echo", port))
	c.Expect(t, "opened", wait)
}

func TestUnassignedOpensAreAborted(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	port := echo(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_echo", port))
	for _, o := range []map[string]any{
		open(1, "bt_other", port),  // unknown target
		open(3, "bt_echo", port+1), // port not assigned
		{"t": "open", "id": 5, "target": "bt_echo", "host": "127.0.0.2", "port": port}, // host not the target's
		{"t": "open", "id": 7, "target": "bt_echo", "host": "localhost", "port": port}, // a name
		open(8, "bt_echo", port), // even id: the hub's ids are odd
	} {
		c.Send(t, o)
		m := c.Expect(t, "abort", wait)
		if fmtInt(int(m.Num("id"))) != fmtAny(o["id"]) {
			t.Fatalf("abort %s for %v", m.Raw, o)
		}
	}
}

func TestHardDeniesEvenWhenAssigned(t *testing.T) {
	hub := fakehub.New(t)
	own := func() ([]netip.Prefix, error) { return []netip.Prefix{netip.MustParsePrefix("192.168.1.20/24")}, nil }
	var dialled atomic.Int32
	startBridge(t, hub, func(r *Runner) {
		r.Policy = policy.New(policy.Options{OwnAddrs: own}) // no test exemption
		r.CameraDial = func(ctx context.Context, n, a string) (net.Conn, error) {
			dialled.Add(1)
			return nil, io.EOF
		}
	})
	port := echo(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil,
		proto.Target{ID: "lo", Host: "127.0.0.1", Ports: []int{port}},
		proto.Target{ID: "ll", Host: "169.254.169.254", Ports: []int{80}},
		proto.Target{ID: "pub", Host: "8.8.8.8", Ports: []int{554}},
		proto.Target{ID: "own", Host: "192.168.1.20", Ports: []int{3389}},
		proto.Target{ID: "mc", Host: "239.255.255.250", Ports: []int{1900}},
	)
	for i, o := range []map[string]any{
		{"t": "open", "id": 1, "target": "lo", "host": "127.0.0.1", "port": port},
		{"t": "open", "id": 3, "target": "ll", "host": "169.254.169.254", "port": 80},
		{"t": "open", "id": 5, "target": "pub", "host": "8.8.8.8", "port": 554},
		{"t": "open", "id": 7, "target": "own", "host": "192.168.1.20", "port": 3389},
		{"t": "open", "id": 9, "target": "mc", "host": "239.255.255.250", "port": 1900},
	} {
		c.Send(t, o)
		m := c.Expect(t, "abort", wait)
		if m.Num("id") != int64(2*i+1) || !strings.HasPrefix(m.Str("reason"), "denied") {
			t.Fatalf("abort %s", m.Raw)
		}
	}
	c.Send(t, map[string]any{"t": "probe", "id": "p1", "target": "ll", "host": "169.254.169.254", "port": 80})
	if m := c.Expect(t, "probe_result", wait); m.Frame["ok"] != false || !strings.HasPrefix(m.Str("error"), "denied") {
		t.Fatalf("probe %s", m.Raw)
	}
	if dialled.Load() != 0 {
		t.Fatal("a denied address was dialled")
	}
}

func TestOwnerAllowList(t *testing.T) {
	hub := fakehub.New(t)
	allow, _ := policy.ParseAllow([]string{"192.168.77.1"})
	startBridge(t, hub, func(r *Runner) {
		r.Policy = policy.New(policy.Options{Exempt: loopback, Allow: allow})
	})
	port := echo(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_echo", port))
	c.Send(t, open(1, "bt_echo", port))
	if m := c.Expect(t, "abort", wait); !strings.Contains(m.Str("reason"), "allow list") {
		t.Fatalf("abort %s", m.Raw)
	}
}

func TestTargetsReplacementAbortsRemovedStreams(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	camA, camB := newCamera(t), newCamera(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_a", camA.port), target("bt_b", camB.port))
	c.Send(t, open(1, "bt_a", camA.port))
	connA := camA.accept(t)
	c.Expect(t, "opened", wait)
	c.Send(t, open(3, "bt_b", camB.port))
	connB := camB.accept(t)
	c.Expect(t, "opened", wait)

	c.Targets(t, 2, target("bt_b", camB.port)) // bt_a is gone
	m := c.Expect(t, "abort", wait)
	if m.Num("id") != 1 || !strings.Contains(m.Str("reason"), "no longer assigned") {
		t.Fatalf("abort %s", m.Raw)
	}
	_ = connA.SetReadDeadline(time.Now().Add(wait))
	if _, err := connA.Read(make([]byte, 1)); !isReset(err) {
		t.Fatalf("camera A: %v, want a reset", err)
	}
	// bt_b carries on
	c.SendChunk(t, 3, []byte("still here"))
	buf := make([]byte, 10)
	_ = connB.SetReadDeadline(time.Now().Add(wait))
	if _, err := io.ReadFull(connB, buf); err != nil || string(buf) != "still here" {
		t.Fatalf("camera B: %q %v", buf, err)
	}
	// and bt_a cannot be opened again
	c.Send(t, open(5, "bt_a", camA.port))
	if m := c.Expect(t, "abort", wait); m.Num("id") != 5 {
		t.Fatalf("abort %s", m.Raw)
	}
}

func TestProbeAndTargetErrors(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	port := echo(t)
	ln, _ := net.Listen("tcp", "127.0.0.1:0") // a port with nothing listening
	closed := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	c := hub.Accept(t, wait)
	c.Welcome(t, map[string]any{"heartbeat_s": 1}, target("bt_echo", port), target("bt_dead", closed))
	c.Send(t, map[string]any{"t": "probe", "id": 42, "target": "bt_echo", "host": "127.0.0.1", "port": port})
	if m := c.Expect(t, "probe_result", wait); m.Num("id") != 42 || m.Frame["ok"] != true || m.Frame["error"] != nil {
		t.Fatalf("probe %s", m.Raw)
	}
	c.Send(t, open(1, "bt_dead", closed))
	if m := c.Expect(t, "abort", wait); !strings.Contains(m.Str("reason"), "refused") {
		t.Fatalf("abort %s", m.Raw)
	}
	key := "bt_dead:" + itoa(closed)
	for deadline := time.Now().Add(wait); time.Now().Before(deadline); {
		m, _ := c.Recv(t, wait)
		if m.T != "heartbeat" {
			continue
		}
		if errs, _ := m.Frame["target_errors"].(map[string]any); errs[key] == "connection refused" {
			return
		}
	}
	t.Fatalf("no target_errors[%s] in heartbeats", key)
}

func itoa(n int) string { return fmtInt(n) }

func fmtAny(v any) string {
	b, _ := proto.Encode(v)
	return string(b)
}

func fmtInt(n int) string {
	b, _ := proto.Encode(n)
	return string(b)
}

func TestAnythingElseFromTheHubIsIgnored(t *testing.T) {
	hub := fakehub.New(t)
	startBridge(t, hub)
	port := echo(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_echo", port))
	c.Send(t, map[string]any{"t": "turn", "urls": []string{"x"}})
	c.SendRaw(t, websocket.MessageText, []byte("not json"))
	c.SendRaw(t, websocket.MessageText, []byte(`["a list"]`))
	c.SendRaw(t, websocket.MessageBinary, []byte{1, 2})
	c.SendChunk(t, 99, []byte("for no stream"))
	c.Send(t, map[string]any{"t": "open", "id": "x"})
	c.Send(t, map[string]any{"t": "opened", "id": 1})
	c.Send(t, map[string]any{"t": "enrolled", "token": "zzz"})
	c.Send(t, map[string]any{"t": "credit", "id": 77, "bytes": -5})
	c.Send(t, map[string]any{"t": "end", "id": 77})
	c.Send(t, map[string]any{"t": "rotate"})
	c.Send(t, map[string]any{"t": "ping", "ts": 1.5})
	if m := c.Expect(t, "pong", wait); m.Frame["ts"] != 1.5 {
		t.Fatalf("pong %s", m.Raw)
	}
	c.Send(t, open(1, "bt_echo", port)) // still fully working
	c.Expect(t, "opened", wait)
}

func TestRotateSavesTokenAndUsesItNextTime(t *testing.T) {
	hub := fakehub.New(t)
	b := startBridge(t, hub)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil)
	hub.AddToken("tok2", "br_test")
	c.Send(t, map[string]any{"t": "rotate", "token": "tok2"})
	c.Expect(t, "rotated", wait)
	if tok, _ := b.tokens.Load(); tok != "tok2" {
		t.Fatalf("token %q", tok)
	}
	c.Close()
	if c2 := hub.Accept(t, wait); c2.Token != "tok2" {
		t.Fatalf("reconnected with %q", c2.Token)
	}
}

func TestRevokedRemovesTokenAndStops(t *testing.T) {
	hub := fakehub.New(t)
	b := startBridge(t, hub)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil)
	c.Send(t, map[string]any{"t": "revoked", "reason": "retired"})
	c.Gone(t, wait)
	if tok, _ := b.tokens.Load(); tok != "" {
		t.Fatal("token kept after revoked")
	}
	if c2 := hub.TryAccept(300 * time.Millisecond); c2 != nil {
		t.Fatal("reconnected without a token")
	}
	if st := b.r.Status(); st.State != "not_enrolled" && st.State != "revoked" {
		t.Fatalf("state %s", st.State)
	}
}

func TestTLSWithCAFile(t *testing.T) {
	hub := fakehub.NewTLS(t)
	ca := filepath.Join(t.TempDir(), "hub-ca.pem")
	if err := os.WriteFile(ca, hub.CertPEM(), 0o644); err != nil {
		t.Fatal(err)
	}
	startBridge(t, hub, func(r *Runner) { r.Config.CAFile = ca })
	hub.Accept(t, wait).Welcome(t, nil)
}

func TestTLSUntrustedCertificateFails(t *testing.T) {
	hub := fakehub.NewTLS(t)
	clk := newFakeClock()
	b := startBridge(t, hub, func(r *Runner) { r.Clock = clk })
	clk.nextSleep(t)
	if hub.Attempts() != 0 {
		t.Fatal("handshake reached the hub despite an untrusted certificate")
	}
	if st := b.r.Status(); !strings.Contains(st.LastError, "certificate") {
		t.Fatalf("last error %q", st.LastError)
	}
}

func TestPlainWSRefusedForRemoteHub(t *testing.T) {
	if _, err := Dial(context.Background(), nil, "ws://hub.axiomvision.ai/bridge", "Bearer x", "t", nil); err == nil {
		t.Fatal("dialled a remote hub over plain ws://")
	}
}

func TestStopIsGracefulAndQuick(t *testing.T) {
	hub := fakehub.New(t)
	b := startBridge(t, hub)
	cam := newCamera(t)
	c := hub.Accept(t, wait)
	c.Welcome(t, nil, target("bt_cam", cam.port))
	c.Send(t, open(1, "bt_cam", cam.port))
	conn := cam.accept(t)
	c.Expect(t, "opened", wait)
	if took := b.stop(); took > 5*time.Second {
		t.Fatalf("stop took %s", took)
	}
	c.Gone(t, wait)
	_ = conn.SetReadDeadline(time.Now().Add(wait))
	if _, err := conn.Read(make([]byte, 1)); !isReset(err) {
		t.Fatalf("camera after stop: %v", err)
	}
	if st := b.r.Status(); st.State != "stopped" {
		t.Fatalf("state %s", st.State)
	}
}

func TestHubOverrunningTheWindowIsAborted(t *testing.T) {
	hub := fakehub.New(t)
	// a camera socket that never accepts a byte (net.Pipe writes block until read), so hub data piles up
	startBridge(t, hub, func(r *Runner) {
		r.CameraDial = func(context.Context, string, string) (net.Conn, error) {
			a, b := net.Pipe()
			t.Cleanup(func() { _ = b.Close() })
			return a, nil
		}
	})
	c := hub.Accept(t, wait)
	c.Welcome(t, map[string]any{"window": 1000}, target("bt_cam", 554))
	c.Send(t, open(1, "bt_cam", 554))
	c.Expect(t, "opened", wait)
	for range 3 { // 1800 bytes against a 1000-byte window, none credited
		c.SendChunk(t, 1, make([]byte, 600))
	}
	if m := c.Expect(t, "abort", wait); !strings.Contains(m.Str("reason"), "window") {
		t.Fatalf("abort %s", m.Raw)
	}
}

func TestQueueOrderAndPriority(t *testing.T) {
	q := newQueue()
	ctx := context.Background()
	_ = q.Data(ctx, []byte("d1"))
	q.Ordered(proto.End{T: "end", ID: 1})
	q.Priority(proto.Credit{T: "credit", ID: 1, Bytes: 2})
	var got []string
	for range 3 {
		it, err := q.next(ctx)
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, string(it.b))
	}
	want := []string{`{"t":"credit","id":1,"bytes":2}`, "d1", `{"t":"end","id":1}`}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order %v", got)
		}
	}
	// data over the cap waits for the writer
	q.dataCap = 4
	_ = q.Data(ctx, []byte("1234"))
	blocked := make(chan error, 1)
	go func() { blocked <- q.Data(ctx, []byte("5")) }()
	select {
	case <-blocked:
		t.Fatal("Data did not wait at the cap")
	case <-time.After(100 * time.Millisecond):
	}
	_, _ = q.next(ctx)
	if err := <-blocked; err != nil {
		t.Fatal(err)
	}
	q.close()
	if err := q.Data(ctx, []byte("x")); err == nil {
		t.Fatal("Data after close")
	}
}
