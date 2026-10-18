package fakehub

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/proto"
)

// Msg is one frame from the bridge: a control frame (T set, Frame decoded) or a chunk (Binary, ID, Data).
type Msg struct {
	T      string
	Frame  map[string]any
	Raw    []byte
	Binary bool
	ID     uint32
	Data   []byte
}

// Num reads a numeric field of a control frame.
func (m Msg) Num(k string) int64 {
	f, _ := m.Frame[k].(float64)
	return int64(f)
}

// Str reads a string field of a control frame.
func (m Msg) Str(k string) string {
	s, _ := m.Frame[k].(string)
	return s
}

// Conn is one bridge connection as the fake hub sees it.
type Conn struct {
	WS       *websocket.Conn
	Token    string
	BridgeID string

	in     chan Msg
	err    chan error
	done   chan struct{}
	once   sync.Once
	ctx    context.Context
	cancel context.CancelFunc
}

func newConn(ws *websocket.Conn, token, id string) *Conn {
	ctx, cancel := context.WithCancel(context.Background())
	c := &Conn{WS: ws, Token: token, BridgeID: id, in: make(chan Msg, 8192), err: make(chan error, 1),
		done: make(chan struct{}), ctx: ctx, cancel: cancel}
	go c.read()
	return c
}

func (c *Conn) read() {
	for {
		typ, b, err := c.WS.Read(c.ctx)
		if err != nil {
			c.err <- err
			close(c.in)
			return
		}
		if typ == websocket.MessageBinary {
			id, data, perr := proto.ParseChunk(b)
			if perr != nil {
				continue
			}
			c.in <- Msg{Binary: true, ID: id, Data: data, Raw: b}
			continue
		}
		m := Msg{Raw: b}
		_ = decodeMap(b, &m.Frame)
		m.T, _ = m.Frame["t"].(string)
		c.in <- m
	}
}

func decodeMap(b []byte, v *map[string]any) error { return json.Unmarshal(b, v) }

// Recv returns the next frame, failing the test on timeout. ok is false once the bridge has disconnected.
func (c *Conn) Recv(t testing.TB, timeout time.Duration) (Msg, bool) {
	t.Helper()
	select {
	case m, ok := <-c.in:
		return m, ok
	case <-time.After(timeout):
		t.Fatalf("no frame from the bridge within %s", timeout)
		return Msg{}, false
	}
}

// Expect skips heartbeats, pongs and credits (unless asked for) until a frame of type typ ("" = a chunk) arrives.
func (c *Conn) Expect(t testing.TB, typ string, timeout time.Duration) Msg {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		left := time.Until(deadline)
		if left <= 0 {
			t.Fatalf("no %q frame within %s", typ, timeout)
		}
		m, ok := c.Recv(t, left)
		if !ok {
			t.Fatalf("bridge disconnected while waiting for %q", typ)
		}
		if (typ == "" && m.Binary) || (typ != "" && m.T == typ) {
			return m
		}
		if m.T == "heartbeat" || m.T == "pong" || m.T == "credit" {
			continue
		}
		t.Fatalf("waiting for %q, got %s", typ, describe(m))
	}
}

// Quiet asserts the bridge sends nothing but heartbeats/pongs for d.
func (c *Conn) Quiet(t testing.TB, d time.Duration) {
	t.Helper()
	deadline := time.After(d)
	for {
		select {
		case m, ok := <-c.in:
			if !ok {
				t.Fatal("bridge disconnected")
			}
			if m.T == "heartbeat" || m.T == "pong" {
				continue
			}
			t.Fatalf("expected silence, got %s", describe(m))
		case <-deadline:
			return
		}
	}
}

// Gone waits for the bridge to drop the connection.
func (c *Conn) Gone(t testing.TB, timeout time.Duration) {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case _, ok := <-c.in:
			if !ok {
				return
			}
		case <-deadline:
			t.Fatalf("bridge still connected after %s", timeout)
		}
	}
}

func describe(m Msg) string {
	if m.Binary {
		return "chunk for stream " + itoa(m.ID) + " (" + itoa(uint32(len(m.Data))) + " bytes)"
	}
	return string(m.Raw)
}

func itoa(n uint32) string {
	b, _ := json.Marshal(n)
	return string(b)
}

// Send writes a control frame (a map or any struct).
func (c *Conn) Send(t testing.TB, frame any) {
	t.Helper()
	b, err := proto.Encode(frame)
	if err != nil {
		t.Fatal(err)
	}
	c.SendRaw(t, websocket.MessageText, b)
}

// SendChunk writes a binary frame for a stream.
func (c *Conn) SendChunk(t testing.TB, id uint32, data []byte) {
	t.Helper()
	c.SendRaw(t, websocket.MessageBinary, proto.ChunkFrame(id, data))
}

// SendRaw writes any frame as is.
func (c *Conn) SendRaw(t testing.TB, typ websocket.MessageType, b []byte) {
	t.Helper()
	ctx, cancel := context.WithTimeout(c.ctx, 5*time.Second)
	defer cancel()
	if err := c.WS.Write(ctx, typ, b); err != nil {
		t.Fatalf("hub write: %v", err)
	}
}

// Welcome reads the bridge's hello and answers with welcome (extra overrides its fields) and the targets.
func (c *Conn) Welcome(t testing.TB, extra map[string]any, targets ...proto.Target) Msg {
	t.Helper()
	hello := c.Expect(t, "hello", 5*time.Second)
	c.SendWelcome(t, extra)
	c.Targets(t, 1, targets...)
	return hello
}

// SendWelcome sends only the welcome (extra overrides its fields), for a client that hangs up once it has it.
func (c *Conn) SendWelcome(t testing.TB, extra map[string]any) {
	t.Helper()
	w := map[string]any{"t": "welcome", "bridge_id": c.BridgeID, "name": "Test bridge", "site": map[string]any{"id": "loc_1", "name": "Test Site"},
		"heartbeat_s": 30, "max_streams": 16, "window": 524288}
	for k, v := range extra {
		w[k] = v
	}
	c.Send(t, w)
}

// Targets sends a full target list.
func (c *Conn) Targets(t testing.TB, rev int, items ...proto.Target) {
	t.Helper()
	if items == nil {
		items = []proto.Target{}
	}
	c.Send(t, map[string]any{"t": "targets", "rev": rev, "items": items})
}

// Close drops the connection from the hub's side.
func (c *Conn) Close() {
	c.once.Do(func() {
		_ = c.WS.CloseNow()
		c.cancel()
		close(c.done)
	})
}
