//go:build windows

package winsvc

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"
)

// ExeName is the installed binary's file name.
const ExeName = "axiom-bridge.exe"

// InstallDir is C:\Program Files\Axiom Bridge.
func InstallDir() string {
	pf := os.Getenv("ProgramFiles")
	if pf == "" {
		pf = `C:\Program Files`
	}
	return filepath.Join(pf, "Axiom Bridge")
}

// IsElevated: install and uninstall need an elevated (Run as administrator) prompt.
func IsElevated() bool { return windows.GetCurrentProcessToken().IsElevated() }

// CopySelf copies the running executable (src) into dir as ExeName, through a temporary file so a failed copy
// never leaves a broken binary. It is a no-op when src already is that file.
func CopySelf(src, dir string) (string, error) {
	dst := filepath.Join(dir, ExeName)
	if same(src, dst) {
		return dst, nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	in, err := os.Open(src)
	if err != nil {
		return "", err
	}
	defer in.Close()
	tmp := dst + ".new"
	out, err := os.Create(tmp)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		_ = os.Remove(tmp)
		return "", err
	}
	if err := out.Close(); err != nil {
		_ = os.Remove(tmp)
		return "", err
	}
	if err := os.Rename(tmp, dst); err != nil {
		_ = os.Remove(tmp)
		return "", fmt.Errorf("replace %s (is the service still running?): %w", dst, err)
	}
	return dst, nil
}

func same(a, b string) bool {
	fa, errA := filepath.Abs(a)
	fb, errB := filepath.Abs(b)
	return errA == nil && errB == nil && strings.EqualFold(filepath.Clean(fa), filepath.Clean(fb))
}

// serviceConfig is how AxiomBridge is registered: automatic (delayed) start as LocalSystem.
func serviceConfig() mgr.Config {
	return mgr.Config{
		ServiceType:      windows.SERVICE_WIN32_OWN_PROCESS,
		StartType:        mgr.StartAutomatic,
		ErrorControl:     mgr.ErrorNormal,
		DisplayName:      DisplayName,
		Description:      Description,
		DelayedAutoStart: true,
		ServiceStartName: "LocalSystem",
	}
}

// recoveryActions restart the service 5, 30 and 60 s after a failure; the failure count resets after a day.
func recoveryActions() ([]mgr.RecoveryAction, uint32) {
	return []mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 5 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 30 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}, 24 * 60 * 60
}

// binaryPath is the service command line: the quoted exe and `run`.
func binaryPath(exe string) string { return syscall.EscapeArg(exe) + " run" }

// Register creates the service (or updates it, on a reinstall/upgrade), sets its recovery actions and the event
// log source. It does not start it.
func Register(exe string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("service manager: %w", err)
	}
	defer m.Disconnect()
	cfg := serviceConfig()
	s, err := m.OpenService(ServiceName)
	if err == nil {
		cfg.BinaryPathName = binaryPath(exe)
		if err := s.UpdateConfig(cfg); err != nil {
			s.Close()
			return fmt.Errorf("update service: %w", err)
		}
	} else {
		s, err = m.CreateService(ServiceName, exe, cfg, "run")
		if err != nil {
			return fmt.Errorf("create service: %w", err)
		}
	}
	defer s.Close()
	actions, reset := recoveryActions()
	if err := s.SetRecoveryActions(actions, reset); err != nil {
		return fmt.Errorf("recovery actions: %w", err)
	}
	if err := s.SetRecoveryActionsOnNonCrashFailures(true); err != nil {
		return fmt.Errorf("recovery actions: %w", err)
	}
	if err := eventlog.InstallAsEventCreate(ServiceName, eventlog.Error|eventlog.Warning|eventlog.Info); err != nil &&
		!strings.Contains(err.Error(), "exists") {
		return fmt.Errorf("event log source: %w", err)
	}
	return nil
}

// Start starts the service and waits (up to timeout) until it reports Running.
func Start(timeout time.Duration) error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	s, err := m.OpenService(ServiceName)
	if err != nil {
		return err
	}
	defer s.Close()
	if err := s.Start(); err != nil {
		return fmt.Errorf("start service: %w", err)
	}
	return waitState(s, svc.Running, timeout)
}

// Stop stops the service if it exists and runs, waiting up to timeout. A missing service is not an error.
func Stop(timeout time.Duration) error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	s, err := m.OpenService(ServiceName)
	if err != nil {
		return nil
	}
	defer s.Close()
	st, err := s.Query()
	if err != nil || st.State == svc.Stopped {
		return err
	}
	if _, err := s.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
		return fmt.Errorf("stop service: %w", err)
	}
	return waitState(s, svc.Stopped, timeout)
}

func waitState(s *mgr.Service, want svc.State, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		st, err := s.Query()
		if err != nil {
			return err
		}
		if st.State == want {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("service is %s, not %s, after %s", stateName(st.State), stateName(want), timeout)
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// Unregister stops and deletes the service and its event log source; a missing service is not an error.
func Unregister() error {
	if err := Stop(15 * time.Second); err != nil {
		return err
	}
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	if s, err := m.OpenService(ServiceName); err == nil {
		err = s.Delete()
		s.Close()
		if err != nil {
			return fmt.Errorf("delete service: %w", err)
		}
	}
	_ = eventlog.Remove(ServiceName)
	return nil
}

// RemoveInstalled deletes the installed binary and its folder. If that binary is the one running (uninstall run
// from Program Files), Windows cannot delete it now, so it is scheduled for deletion at the next reboot.
func RemoveInstalled(running string) (deferred bool, err error) {
	dir := InstallDir()
	exe := filepath.Join(dir, ExeName)
	if same(running, exe) {
		p, _ := windows.UTF16PtrFromString(exe)
		if err := windows.MoveFileEx(p, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT); err != nil {
			return false, err
		}
		d, _ := windows.UTF16PtrFromString(dir)
		_ = windows.MoveFileEx(d, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT)
		return true, nil
	}
	if err := os.Remove(exe); err != nil && !errors.Is(err, os.ErrNotExist) {
		return false, err
	}
	_ = os.Remove(exe + ".new")
	_ = os.Remove(dir) // only if empty
	return false, nil
}

// QueryState is the service's state as text ("running", "stopped", "not installed"). It needs no elevation: it
// asks only for SERVICE_QUERY_STATUS.
func QueryState(name string) (string, error) {
	scm, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_CONNECT)
	if err != nil {
		return "", err
	}
	defer windows.CloseServiceHandle(scm)
	n, _ := windows.UTF16PtrFromString(name)
	h, err := windows.OpenService(scm, n, windows.SERVICE_QUERY_STATUS)
	if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) {
		return "not installed", nil
	}
	if err != nil {
		return "", err
	}
	defer windows.CloseServiceHandle(h)
	var st windows.SERVICE_STATUS
	if err := windows.QueryServiceStatus(h, &st); err != nil {
		return "", err
	}
	return stateName(svc.State(st.CurrentState)), nil
}

func stateName(s svc.State) string {
	switch s {
	case svc.Stopped:
		return "stopped"
	case svc.StartPending:
		return "starting"
	case svc.StopPending:
		return "stopping"
	case svc.Running:
		return "running"
	case svc.ContinuePending:
		return "continuing"
	case svc.PausePending:
		return "pausing"
	case svc.Paused:
		return "paused"
	}
	return fmt.Sprintf("state %d", s)
}
