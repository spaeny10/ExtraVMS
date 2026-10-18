//go:build windows

package winsvc

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

// These tests exercise the service code without registering anything with the SCM.

func runHandler(t *testing.T, run func(context.Context) error) (chan svc.ChangeRequest, chan svc.Status, chan [2]any) {
	t.Helper()
	req := make(chan svc.ChangeRequest)
	status := make(chan svc.Status, 16)
	out := make(chan [2]any, 1)
	h := &handler{run: run, stopWait: StopWait}
	go func() {
		ssec, code := h.Execute(nil, req, status)
		out <- [2]any{ssec, code}
	}()
	if s := <-status; s.State != svc.StartPending {
		t.Fatalf("first status %v", s.State)
	}
	if s := <-status; s.State != svc.Running || s.Accepts&svc.AcceptStop == 0 || s.Accepts&svc.AcceptShutdown == 0 {
		t.Fatalf("running status %+v", s)
	}
	return req, status, out
}

func TestStopCancelsAndReturnsQuickly(t *testing.T) {
	stopped := make(chan struct{})
	req, status, out := runHandler(t, func(ctx context.Context) error {
		<-ctx.Done()
		time.Sleep(100 * time.Millisecond) // a little cleanup
		close(stopped)
		return nil
	})
	cur := svc.Status{State: svc.Running}
	req <- svc.ChangeRequest{Cmd: svc.Interrogate, CurrentStatus: cur}
	if s := <-status; s.State != svc.Running {
		t.Fatalf("interrogate %v", s.State)
	}
	start := time.Now()
	req <- svc.ChangeRequest{Cmd: svc.Stop}
	if s := <-status; s.State != svc.StopPending {
		t.Fatalf("stop status %v", s.State)
	}
	res := <-out
	if took := time.Since(start); took > 5*time.Second {
		t.Fatalf("stop took %s", took)
	}
	<-stopped
	if res[0] != false || res[1] != uint32(0) {
		t.Fatalf("exit %v", res)
	}
}

func TestStopGivesUpOnAHungBridge(t *testing.T) {
	req, status, out := runHandler(t, func(ctx context.Context) error {
		select {} // never returns
	})
	start := time.Now()
	req <- svc.ChangeRequest{Cmd: svc.Shutdown}
	<-status
	<-out
	if took := time.Since(start); took < StopWait-100*time.Millisecond || took > 5*time.Second {
		t.Fatalf("hung stop took %s", took)
	}
}

func TestUnexpectedExitIsAFailure(t *testing.T) {
	_, status, out := runHandler(t, func(ctx context.Context) error { return os.ErrClosed })
	<-status
	res := <-out
	if res[0] != true || res[1] != uint32(1) {
		t.Fatalf("exit %v: the recovery actions need a failure", res)
	}
}

func TestServiceRegistrationValues(t *testing.T) {
	c := serviceConfig()
	if c.StartType != mgr.StartAutomatic || !c.DelayedAutoStart || c.ServiceStartName != "LocalSystem" || c.DisplayName != "Axiom Bridge" {
		t.Fatalf("config %+v", c)
	}
	acts, reset := recoveryActions()
	if len(acts) != 3 || acts[0].Delay != 5*time.Second || acts[1].Delay != 30*time.Second || acts[2].Delay != 60*time.Second || reset != 86400 {
		t.Fatalf("recovery %+v %d", acts, reset)
	}
	for _, a := range acts {
		if a.Type != mgr.ServiceRestart {
			t.Fatalf("action %+v", a)
		}
	}
	if got := binaryPath(`C:\Program Files\Axiom Bridge\axiom-bridge.exe`); got != `"C:\Program Files\Axiom Bridge\axiom-bridge.exe" run` {
		t.Fatalf("binary path %s", got)
	}
}

func TestCopySelf(t *testing.T) {
	src := filepath.Join(t.TempDir(), "source.exe")
	if err := os.WriteFile(src, []byte("binary v1"), 0o755); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(t.TempDir(), "Axiom Bridge")
	dst, err := CopySelf(src, dir)
	if err != nil || filepath.Base(dst) != ExeName {
		t.Fatalf("%s %v", dst, err)
	}
	if b, _ := os.ReadFile(dst); string(b) != "binary v1" {
		t.Fatalf("copied %q", b)
	}
	_ = os.WriteFile(src, []byte("binary v2"), 0o755) // an upgrade replaces it
	if _, err := CopySelf(src, dir); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(dst); string(b) != "binary v2" {
		t.Fatalf("upgraded %q", b)
	}
	if again, err := CopySelf(dst, dir); err != nil || again != dst { // running from the install dir: no-op
		t.Fatalf("%s %v", again, err)
	}
}

func TestQueryStateNeedsNoElevation(t *testing.T) {
	if st, err := QueryState("AxiomBridgeTestNoSuchService"); err != nil || st != "not installed" {
		t.Fatalf("%q %v", st, err)
	}
	if st, err := QueryState("EventLog"); err != nil || st != "running" { // a service every Windows runs
		t.Fatalf("EventLog: %q %v", st, err)
	}
}
