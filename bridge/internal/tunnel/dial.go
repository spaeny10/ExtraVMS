// Package tunnel keeps the bridge's one WebSocket to the hub: dial (TLS with the system roots), hello, the writer
// goroutine and its queues, keepalive, heartbeats, and reconnecting with backoff when anything goes wrong.
package tunnel

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"math/rand/v2"
	"net"
	"net/http"
	"os"
	"runtime"
	"time"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/policy"
	"axiomvision.ai/bridge/internal/proto"
)

// AuthError is a handshake the hub refused with 401 or 403: a bad token or enrollment code. Retrying quickly would
// not help, so the bridge waits AuthRetry.
type AuthError struct{ Status int }

func (e *AuthError) Error() string {
	return fmt.Sprintf("hub refused the credentials (HTTP %d)", e.Status)
}

// IsAuth reports whether err is an AuthError.
func IsAuth(err error) bool {
	var a *AuthError
	return errors.As(err, &a)
}

// HTTPClient is the client for the WebSocket handshake: the OS's trusted roots plus the optional ca_file, TLS 1.2+,
// HTTP/1.1 only (WebSockets need it), and the usual proxy environment variables.
func HTTPClient(cfg config.Config) (*http.Client, error) {
	tc := &tls.Config{MinVersion: tls.VersionTLS12}
	if cfg.CAFile != "" {
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("ca_file: %w", err)
		}
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("ca_file %s: no PEM certificates in it", cfg.CAFile)
		}
		tc.RootCAs = pool
	}
	tr := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		DialContext:         (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSClientConfig:     tc,
		TLSHandshakeTimeout: 15 * time.Second,
		ForceAttemptHTTP2:   false,
	}
	return &http.Client{Transport: tr}, nil
}

// Dial opens the WebSocket with the given Authorization ("Enroll <code>" or "Bearer <token>"). A 401/403 at the
// handshake comes back as *AuthError. The credential is only ever in the header, never in a log line or error.
func Dial(ctx context.Context, client *http.Client, hubURL, auth, version string, onPong func()) (*websocket.Conn, error) {
	if err := config.CheckHubURL(hubURL); err != nil {
		return nil, err
	}
	h := http.Header{}
	h.Set("Authorization", auth)
	h.Set("User-Agent", "axiom-bridge/"+version)
	opts := &websocket.DialOptions{HTTPClient: client, HTTPHeader: h, CompressionMode: websocket.CompressionDisabled}
	if onPong != nil {
		opts.OnPongReceived = func(context.Context, []byte) { onPong() }
	}
	ws, resp, err := websocket.Dial(ctx, hubURL, opts)
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
			return nil, &AuthError{Status: resp.StatusCode}
		}
		return nil, err
	}
	ws.SetReadLimit(proto.MaxText + 1024)
	return ws, nil
}

// Hello is the bridge's first frame on every connection.
func Hello(version string) proto.Hello {
	host, _ := os.Hostname()
	return proto.Hello{T: "hello", Proto: proto.Version, Version: version, OS: runtime.GOOS, Arch: runtime.GOARCH,
		Hostname: host, LAN: policy.LAN(), Caps: []string{"tcp", "probe"}}
}

// Backoff is full-jitter exponential backoff: a random wait in [0, min(Max, Base·2^attempt)).
type Backoff struct {
	Base, Max time.Duration
	Rand      func() float64 // [0, 1); nil = math/rand
	attempt   int
}

// Next is the wait before the next attempt.
func (b *Backoff) Next() time.Duration {
	ceil := b.Base << min(b.attempt, 30)
	if ceil <= 0 || ceil > b.Max {
		ceil = b.Max
	}
	b.attempt++
	r := b.Rand
	if r == nil {
		r = rand.Float64
	}
	return time.Duration(r() * float64(ceil))
}

// Reset starts again from Base (after a connection that stayed up long enough).
func (b *Backoff) Reset() { b.attempt = 0 }

// Clock is time for the reconnect loop; tests inject a fake one so backoff runs instantly.
type Clock interface {
	Now() time.Time
	Sleep(ctx context.Context, d time.Duration) error
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

func (realClock) Sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
