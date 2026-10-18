package enroll

import (
	"context"
	"errors"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/fakehub"
	"axiomvision.ai/bridge/internal/proto"
	"axiomvision.ai/bridge/internal/tunnel"
)

func tokenFile(t *testing.T) config.TokenFile {
	t.Helper()
	sddl, err := config.UserTokenSDDL() // the test user must read it back; the default is SYSTEM + Administrators
	if err != nil {
		t.Fatal(err)
	}
	return config.TokenFile{Path: filepath.Join(t.TempDir(), "token"), SDDL: sddl}
}

func TestEnrollWritesTokenAndCodeIsSingleUse(t *testing.T) {
	hub := fakehub.New(t)
	hub.AddCode("AXB-ABCD-EFGH-JKLM", "br_1")
	tf := tokenFile(t)
	ctx := context.Background()

	id, err := Enroll(ctx, nil, hub.URL, " axb-abcd-efgh-jklm \n", "test", tf) // pasted with spaces, lower case
	if err != nil || id != "br_1" {
		t.Fatalf("enroll: %q %v", id, err)
	}
	tok, err := tf.Load()
	if err != nil || !strings.HasPrefix(tok, "tok-br_1-") {
		t.Fatalf("token %q %v", tok, err)
	}
	if runtime.GOOS == "windows" {
		if sddl, _ := config.FileSDDL(tf.Path); !strings.HasPrefix(sddl, "D:P(A;;FA;;;SY)(A;;FA;;;BA)") {
			t.Fatalf("token DACL %s", sddl)
		}
	}
	if hub.EnrollHello["t"] != "hello" || hub.EnrollHello["version"] != "test" {
		t.Fatalf("hello on enrollment: %v", hub.EnrollHello)
	}
	if auths := hub.Auths(); len(auths) != 1 || auths[0] != "Enroll AXB-ABCD-EFGH-JKLM" {
		t.Fatalf("auth headers %v", auths)
	}

	// the code is spent: a second use is refused at the handshake and the saved token is untouched
	other := tokenFile(t)
	if _, err := Enroll(ctx, nil, hub.URL, "AXB-ABCD-EFGH-JKLM", "test", other); !errors.Is(err, ErrCodeRefused) {
		t.Fatalf("reuse: %v", err)
	}
	if got, _ := other.Load(); got != "" {
		t.Fatal("token written for a refused code")
	}
	if got, _ := tf.Load(); got != tok {
		t.Fatal("first token changed")
	}

	// and the new token is accepted for a normal connection, whose welcome install prints. Verify runs in the
	// background (it blocks until the welcome) while this goroutine plays the hub, so t.Fatal stays on the test
	// goroutine. The hub sends only the welcome: Verify hangs up as soon as it has it, so anything the hub wrote
	// after it (the real hub's `targets`) could meet a closed socket.
	r := &tunnel.Runner{Config: config.Config{HubURL: hub.URL}, Tokens: tf, Version: "test"}
	type result struct {
		w   proto.Welcome
		err error
	}
	res := make(chan result, 1)
	go func() {
		w, err := r.Verify(ctx)
		res <- result{w, err}
	}()
	c := hub.Accept(t, 5*time.Second)
	c.Expect(t, "hello", 5*time.Second)
	c.SendWelcome(t, map[string]any{"name": "Gate PC", "site": "Ranch"})
	c.Gone(t, 5*time.Second) // Verify closes after the welcome
	got := <-res
	w, err := got.w, got.err
	if err != nil || w.Name != "Gate PC" || w.SiteName() != "Ranch" || w.BridgeID != "br_1" {
		t.Fatalf("verify: %+v %v", w, err)
	}
}

func TestEnrollUnreachableHub(t *testing.T) {
	hub := fakehub.New(t)
	url := hub.URL
	hub.Close()
	_, err := Enroll(context.Background(), nil, url, "AXB-1", "test", tokenFile(t))
	if err == nil || errors.Is(err, ErrCodeRefused) || !strings.Contains(err.Error(), "cannot reach the hub") {
		t.Fatalf("err %v", err)
	}
	if _, err := Enroll(context.Background(), nil, "ws://example.com/bridge", "AXB-1", "test", tokenFile(t)); err == nil {
		t.Fatal("enrolled over plain ws:// to a remote hub")
	}
	if _, err := Enroll(context.Background(), nil, url, "  ", "test", tokenFile(t)); err == nil {
		t.Fatal("empty code accepted")
	}
}
