// Package fakehub is a stand-in for the hub's WS /bridge endpoint, for tests: an httptest server that enrolls with
// single-use codes, accepts Bearer tokens, and hands each live connection to the test, which then plays the hub's
// side frame by frame. It never talks to a real hub.
package fakehub

import (
	"context"
	"encoding/pem"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/proto"
)

// Hub is the fake hub.
type Hub struct {
	Server *httptest.Server
	URL    string // ws://127.0.0.1:port/bridge (wss:// with NewTLS)

	NoPong atomic.Bool // stop answering WebSocket pings (a hub that has gone silent)

	mu          sync.Mutex
	codes       map[string]string // unused enrollment code → bridge id
	tokens      map[string]string // device token → bridge id
	issued      int
	EnrollHello map[string]any // the hello received on the last enrollment
	conns       chan *Conn
	live        []*Conn
	attempts    atomic.Int32
	auths       []string // scheme of each handshake ("Bearer", "Enroll", …)
}

// New starts a plain ws:// fake hub on 127.0.0.1.
func New(t testing.TB) *Hub { return start(t, false) }

// NewTLS starts a wss:// fake hub with a self-signed certificate (CertPEM gives it, for ca_file).
func NewTLS(t testing.TB) *Hub { return start(t, true) }

func start(t testing.TB, tls bool) *Hub {
	h := &Hub{codes: map[string]string{}, tokens: map[string]string{}, conns: make(chan *Conn, 16)}
	mux := http.NewServeMux()
	mux.HandleFunc("/bridge", h.serve)
	h.Server = httptest.NewUnstartedServer(mux)
	h.Server.Config.ErrorLog = log.New(io.Discard, "", 0) // refused TLS handshakes are expected in tests
	if tls {
		h.Server.StartTLS()
		h.URL = "wss" + strings.TrimPrefix(h.Server.URL, "https") + "/bridge"
	} else {
		h.Server.Start()
		h.URL = "ws" + strings.TrimPrefix(h.Server.URL, "http") + "/bridge"
	}
	t.Cleanup(h.Close)
	return h
}

// CertPEM is the TLS server's certificate.
func (h *Hub) CertPEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: h.Server.Certificate().Raw})
}

// AddCode makes an enrollment code valid (once) for a bridge id.
func (h *Hub) AddCode(code, bridgeID string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.codes[code] = bridgeID
}

// AddToken makes a device token valid.
func (h *Hub) AddToken(token, bridgeID string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.tokens[token] = bridgeID
}

// RemoveToken revokes a device token (later handshakes with it get 401).
func (h *Hub) RemoveToken(token string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.tokens, token)
}

// Attempts is how many handshakes reached /bridge.
func (h *Hub) Attempts() int { return int(h.attempts.Load()) }

// Auths lists the Authorization header of every handshake so far.
func (h *Hub) Auths() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.auths...)
}

// Close drops every connection and stops the server.
func (h *Hub) Close() {
	h.mu.Lock()
	live := h.live
	h.live = nil
	h.mu.Unlock()
	for _, c := range live {
		c.Close()
	}
	h.Server.Close()
}

func (h *Hub) serve(w http.ResponseWriter, r *http.Request) {
	h.attempts.Add(1)
	auth := r.Header.Get("Authorization")
	scheme, cred, _ := strings.Cut(auth, " ")
	h.mu.Lock()
	h.auths = append(h.auths, auth)
	h.mu.Unlock()
	switch scheme {
	case "Enroll":
		h.enroll(w, r, strings.TrimSpace(cred))
	case "Bearer":
		h.mu.Lock()
		id, ok := h.tokens[cred]
		h.mu.Unlock()
		if !ok {
			http.Error(w, "unknown token", http.StatusUnauthorized)
			return
		}
		ws, err := websocket.Accept(w, r, &websocket.AcceptOptions{
			OnPingReceived: func(context.Context, []byte) bool { return !h.NoPong.Load() },
		})
		if err != nil {
			return
		}
		ws.SetReadLimit(4 << 20)
		c := newConn(ws, cred, id)
		h.mu.Lock()
		h.live = append(h.live, c)
		h.mu.Unlock()
		h.conns <- c
		<-c.done
	default:
		http.Error(w, "authorization required", http.StatusUnauthorized)
	}
}

// enroll mirrors hub/hub/agents.py _enroll: refuse a bad code at the handshake, else wait for hello, consume the
// code, send `enrolled` and close.
func (h *Hub) enroll(w http.ResponseWriter, r *http.Request, code string) {
	h.mu.Lock()
	id, ok := h.codes[code]
	h.mu.Unlock()
	if !ok {
		http.Error(w, "invalid enrollment code", http.StatusForbidden)
		return
	}
	ws, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer ws.CloseNow()
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	var hello map[string]any
	if typ, b, err := ws.Read(ctx); err == nil && typ == websocket.MessageText {
		_ = decodeMap(b, &hello)
	}
	h.mu.Lock()
	if _, still := h.codes[code]; !still { // consumed meanwhile
		h.mu.Unlock()
		_ = ws.Close(4403, "invalid enrollment token")
		return
	}
	delete(h.codes, code)
	h.issued++
	token := fmt.Sprintf("tok-%s-%d", id, h.issued)
	h.tokens[token] = id
	h.EnrollHello = hello
	h.mu.Unlock()
	b, _ := proto.Encode(map[string]any{"t": "enrolled", "bridge_id": id, "token": token})
	_ = ws.Write(ctx, websocket.MessageText, b)
	_ = ws.Close(websocket.StatusNormalClosure, "enrolled")
}

// Accept waits for the next Bearer connection.
func (h *Hub) Accept(t testing.TB, timeout time.Duration) *Conn {
	t.Helper()
	select {
	case c := <-h.conns:
		return c
	case <-time.After(timeout):
		t.Fatalf("no bridge connection within %s", timeout)
		return nil
	}
}

// TryAccept is Accept without failing: nil when nothing connected in time.
func (h *Hub) TryAccept(timeout time.Duration) *Conn {
	select {
	case c := <-h.conns:
		return c
	case <-time.After(timeout):
		return nil
	}
}
