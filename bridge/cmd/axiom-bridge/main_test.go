package main

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"axiomvision.ai/bridge/internal/config"
)

func TestFlagsOverrideConfig(t *testing.T) {
	cfg := config.Config{HubURL: config.DefaultHubURL}
	c := common{hub: "ws://127.0.0.1:8080/bridge", allow: listFlag{"192.168.1.50", "10.0.0.0/8"}, logLevel: "debug"}
	if err := c.apply(&cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.HubURL != "ws://127.0.0.1:8080/bridge" || len(cfg.Allow) != 2 || cfg.LogLevel != "debug" || cfg.AllowPublic {
		t.Fatalf("%+v", cfg)
	}
	bad := common{allow: listFlag{"camera.local"}}
	if bad.apply(&cfg) == nil {
		t.Fatal("accepted a name in --allow")
	}
	remote := common{hub: "ws://hub.example.com/bridge"}
	if remote.apply(&cfg) == nil {
		t.Fatal("accepted plain ws:// to a remote hub")
	}
}

func TestPrintStatus(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	st := config.Status{Version: "0.1.0", PID: 42, Hub: config.DefaultHubURL, State: "connected", Connected: true,
		Since: now.Add(-time.Hour), BridgeID: "br_1", Name: "Gate PC", Site: "Ranch", Streams: 3, Targets: 1, TargetsRev: 4,
		BytesUp: 3 << 30, BytesDown: 1500, UpdatedAt: now.Add(-10 * time.Second),
		TargetErrors: map[string]string{"bt_1:555": "connection refused"}}
	var b bytes.Buffer
	printStatus(&b, st, now)
	out := b.String()
	for _, want := range []string{"connected since", "1h0m0s ago", "Gate PC (br_1)", "Site: Ranch", "3 open", "3.0 GiB", "1.5 KiB", "bt_1:555: connection refused"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	st.Connected, st.State, st.UpdatedAt = false, "waiting", now.Add(-time.Hour)
	st.LastError, st.LastErrorAt = "hub refused the credentials (HTTP 401)", now.Add(-time.Minute)
	b.Reset()
	printStatus(&b, st, now)
	if !strings.Contains(b.String(), "stale") || !strings.Contains(b.String(), "HTTP 401") {
		t.Fatal(b.String())
	}
}
