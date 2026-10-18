package config

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestCheckHubURL(t *testing.T) {
	for _, ok := range []string{"wss://hub.axiomvision.ai/bridge", "ws://localhost:8080/bridge", "ws://127.0.0.1:1/bridge", "ws://[::1]:9/bridge", "wss://10.0.0.1/bridge"} {
		if err := CheckHubURL(ok); err != nil {
			t.Errorf("%s: %v", ok, err)
		}
	}
	for _, bad := range []string{"ws://hub.axiomvision.ai/bridge", "http://localhost/bridge", "https://hub/bridge", "wss:///bridge", "ws://10.0.0.1/bridge", "::"} {
		if CheckHubURL(bad) == nil {
			t.Errorf("%s accepted", bad)
		}
	}
}

func TestLoadSave(t *testing.T) {
	dir := t.TempDir()
	p := PathsIn(dir)
	c, err := Load(p.Config)
	if err != nil || c.HubURL != DefaultHubURL {
		t.Fatalf("defaults: %+v %v", c, err)
	}
	c = Config{HubURL: "ws://127.0.0.1:9/bridge", Allow: []string{"192.168.1.50"}, LogLevel: "debug"}
	if err := Save(p.Config, c); err != nil {
		t.Fatal(err)
	}
	got, err := Load(p.Config)
	if err != nil || got.HubURL != c.HubURL || len(got.Allow) != 1 || got.LogLevel != "debug" {
		t.Fatalf("round trip: %+v %v", got, err)
	}
	if Save(p.Config, Config{HubURL: "ws://example.com/bridge"}) == nil {
		t.Fatal("saved a plain ws:// remote hub")
	}
	if err := os.WriteFile(p.Config, []byte(`{"hub_url":"ws://example.com/bridge"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(p.Config); err == nil {
		t.Fatal("loaded a plain ws:// remote hub")
	}
}

func TestTokenFileDefaultACL(t *testing.T) {
	dir := t.TempDir()
	tf := TokenFile{Path: filepath.Join(dir, "token")}
	unlock(t, tf.Path)
	if tok, err := tf.Load(); err != nil || tok != "" {
		t.Fatalf("missing token: %q %v", tok, err)
	}
	if err := tf.Save("secret-token"); err != nil {
		t.Fatal(err)
	}
	sddl, err := FileSDDL(tf.Path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		// protected (P): nothing inherited; SYSTEM and Administrators only
		if sddl != DefaultTokenSDDL {
			t.Fatalf("token DACL %s, want %s", sddl, DefaultTokenSDDL)
		}
		// an unelevated process (this test) cannot read it back
		if _, err := os.ReadFile(tf.Path); err == nil && !elevated() {
			t.Fatal("token readable by an unelevated user")
		}
	} else if sddl != "-rw-------" {
		t.Fatalf("token mode %s", sddl)
	}
	leftovers, _ := filepath.Glob(filepath.Join(dir, ".token*"))
	if len(leftovers) != 0 {
		t.Fatalf("temporary files left: %v", leftovers)
	}
}

func TestTokenFileUserACLRoundTrip(t *testing.T) {
	sddl, err := UserTokenSDDL()
	if err != nil {
		t.Fatal(err)
	}
	tf := TokenFile{Path: filepath.Join(t.TempDir(), "sub", "token"), SDDL: sddl}
	for _, tok := range []string{"first", "second"} { // the second replaces the first atomically
		if err := tf.Save(tok); err != nil {
			t.Fatal(err)
		}
		got, err := tf.Load()
		if err != nil || got != tok {
			t.Fatalf("load %q %v, want %q", got, err, tok)
		}
	}
	if runtime.GOOS == "windows" {
		got, _ := FileSDDL(tf.Path)
		if !strings.HasPrefix(got, "D:P(A;;FA;;;SY)(A;;FA;;;BA)") {
			t.Fatalf("DACL %s", got)
		}
	}
	if tf.Save("  ") == nil {
		t.Fatal("saved an empty token")
	}
	if err := tf.Remove(); err != nil {
		t.Fatal(err)
	}
	if err := tf.Remove(); err != nil {
		t.Fatal("second remove:", err)
	}
}

func TestStatusRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	now := time.Now().UTC().Truncate(time.Second)
	in := Status{Version: "1", State: "connected", Connected: true, Since: now, Name: "Gate PC", Streams: 2, UpdatedAt: now}
	if err := WriteStatus(path, in); err != nil {
		t.Fatal(err)
	}
	out, err := ReadStatus(path)
	if err != nil || out.Name != "Gate PC" || !out.Since.Equal(now) || out.Streams != 2 {
		t.Fatalf("%+v %v", out, err)
	}
	b, _ := os.ReadFile(path)
	if strings.Contains(string(b), "last_error_at") {
		t.Fatal("zero time written")
	}
}
