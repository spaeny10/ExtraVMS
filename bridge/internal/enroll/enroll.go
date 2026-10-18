// Package enroll turns a one-time enrollment code (created on the hub for a Site) into the bridge's device token:
// connect with `Authorization: Enroll <code>`, send hello, receive `enrolled {bridge_id, token}`, and the hub
// closes (the same exchange as a central host's enrollment in hub/hub/agents.py _enroll). The code is consumed
// atomically on the hub, so a second use is refused at the handshake.
package enroll

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/proto"
	"axiomvision.ai/bridge/internal/tunnel"
)

// ErrCodeRefused is the hub refusing the code at the handshake: unknown, already used or expired.
var ErrCodeRefused = errors.New("the hub refused the enrollment code (unknown, already used or expired): create a new one on the hub")

// Timeout bounds the whole exchange.
const Timeout = 30 * time.Second

// NormalizeCode trims and upper-cases what the admin typed or pasted ("axb-abcd-…" works too).
func NormalizeCode(code string) string { return strings.ToUpper(strings.TrimSpace(code)) }

// Enroll performs the exchange and saves the token; it returns the bridge id the hub assigned.
func Enroll(ctx context.Context, client *http.Client, hubURL, code, version string, tokens tunnel.TokenStore) (string, error) {
	code = NormalizeCode(code)
	if code == "" {
		return "", errors.New("no enrollment code")
	}
	ctx, cancel := context.WithTimeout(ctx, Timeout)
	defer cancel()
	ws, err := tunnel.Dial(ctx, client, hubURL, "Enroll "+code, version, nil)
	if tunnel.IsAuth(err) {
		return "", ErrCodeRefused
	}
	if err != nil {
		return "", fmt.Errorf("cannot reach the hub at %s: %w", hubURL, err)
	}
	defer ws.CloseNow()
	b, _ := proto.Encode(tunnel.Hello(version))
	if err := ws.Write(ctx, websocket.MessageText, b); err != nil {
		return "", fmt.Errorf("sending hello: %w", err)
	}
	for {
		typ, data, err := ws.Read(ctx)
		if err != nil {
			if cs := websocket.CloseStatus(err); cs == 4403 || cs == 4401 || cs == websocket.StatusPolicyViolation {
				return "", ErrCodeRefused // refused after the hello (consumed by someone else meanwhile)
			}
			return "", fmt.Errorf("the hub closed before enrolling this bridge: %w", err)
		}
		if typ != websocket.MessageText {
			continue
		}
		if t, _ := proto.TypeOf(data); t != "enrolled" {
			continue
		}
		var e proto.Enrolled
		if err := proto.Decode(data, &e); err != nil || e.Token == "" {
			return "", fmt.Errorf("the hub's enrolled frame has no token")
		}
		if err := tokens.Save(e.Token); err != nil {
			// the code is spent on the hub: the admin must make a new one after fixing the disk problem
			return e.BridgeID, fmt.Errorf("enrolled as %s but could not save the token: %w", e.BridgeID, err)
		}
		_ = ws.Close(websocket.StatusNormalClosure, "enrolled")
		return e.BridgeID, nil
	}
}
