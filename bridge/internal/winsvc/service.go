//go:build windows

// Package winsvc is the Windows service side of the bridge: the SCM handler that runs the bridge and stops it
// within 5 s, and the install/uninstall primitives (service AxiomBridge, automatic delayed start as LocalSystem,
// restart on failure after 5/30/60 s, event log source). The install sequence itself (copy, config, enroll, start)
// lives in cmd/axiom-bridge, which owns the configuration.
package winsvc

import (
	"context"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
)

const (
	ServiceName = "AxiomBridge"
	DisplayName = "Axiom Bridge"
	Description = "Carries camera connections from this site to the Axiom Vision hub (outbound TCP 443 only)."

	// StopWait is how long a stop waits for the bridge to finish (the SCM is told 5 s).
	StopWait = 4500 * time.Millisecond
)

// IsService reports whether this process was started by the service control manager.
func IsService() bool {
	ok, err := svc.IsWindowsService()
	return err == nil && ok
}

// Run serves the SCM until it stops the service; run gets a context that is cancelled on Stop or Shutdown.
func Run(run func(ctx context.Context) error) error {
	elog, _ := eventlog.Open(ServiceName)
	if elog != nil {
		defer elog.Close()
	}
	return svc.Run(ServiceName, &handler{run: run, stopWait: StopWait, elog: elog})
}

// handler adapts the bridge's Run to svc.Handler.
type handler struct {
	run      func(ctx context.Context) error
	stopWait time.Duration
	elog     *eventlog.Log
}

func (h *handler) event(errorLevel bool, msg string) {
	if h.elog == nil {
		return
	}
	if errorLevel {
		_ = h.elog.Error(1, msg)
	} else {
		_ = h.elog.Info(1, msg)
	}
}

// Execute starts the bridge, answers interrogations, and on Stop/Shutdown cancels it and waits at most stopWait.
// If the bridge returns by itself the service exits with a service-specific error, so the recovery actions
// restart it.
func (h *handler) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- h.run(ctx) }()
	running := svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	status <- running
	h.event(false, "Axiom Bridge started")
	for {
		select {
		case err := <-done:
			msg := "Axiom Bridge stopped unexpectedly"
			if err != nil {
				msg += ": " + err.Error()
			}
			h.event(true, msg)
			status <- svc.Status{State: svc.StopPending}
			return true, 1
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending, WaitHint: 5000}
				cancel()
				select {
				case <-done:
				case <-time.After(h.stopWait):
					h.event(true, "Axiom Bridge did not stop in time; exiting anyway")
				}
				h.event(false, "Axiom Bridge stopped")
				return false, 0
			}
		}
	}
}
