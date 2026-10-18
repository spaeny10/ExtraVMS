// Command axiom-bridge is the Axiom Bridge: a small program on a PC at a camera site with no inbound access
// (Starlink, cellular) that dials the Axiom Vision hub over TCP 443 and carries the hub's streams to the cameras it
// was assigned. It never listens on any port. See bridge/README.md and docs/axiom-bridge-plan.md.
//
//	axiom-bridge install --code AXB-XXXX-XXXX-XXXX [--hub wss://…/bridge] [--allow 192.168.1.50]   (Windows, elevated)
//	axiom-bridge uninstall [--purge]                                                              (Windows, elevated)
//	axiom-bridge run [--code …] [--hub …] [--allow …] [--dir DIR]                                 (foreground; the service)
//	axiom-bridge status
//	axiom-bridge version
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/enroll"
	"axiomvision.ai/bridge/internal/logx"
	"axiomvision.ai/bridge/internal/policy"
	"axiomvision.ai/bridge/internal/tunnel"
)

// version is set at build time: -ldflags "-X main.version=…" (bridge/build.ps1).
var version = "dev"

const usage = `Axiom Bridge %s: carries camera connections from this site to the Axiom Vision hub.

Usage:
  axiom-bridge install --code AXB-XXXX-XXXX-XXXX [--hub URL] [--allow IP[,IP…]] [--allow-public] [--ca-file PEM]
        Windows, from an elevated prompt: install to Program Files, enroll, and start the AxiomBridge service.
        Run again without --code to upgrade (the existing enrollment is kept).
  axiom-bridge uninstall [--purge]
        Windows, elevated: stop and remove the service. --purge also deletes the config, token and logs.
  axiom-bridge run [--code CODE] [--hub URL] [--allow IP] [--allow-public] [--ca-file PEM] [--dir DIR]
        Run in the foreground (testing, Linux under systemd). --code enrolls first; --dir keeps all files there.
  axiom-bridge status
        The service state and the last connection (exit code 0 when connected).
  axiom-bridge version
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, usage, version)
		os.Exit(2)
	}
	var err error
	code := 0
	switch cmd, args := os.Args[1], os.Args[2:]; cmd {
	case "install":
		err = cmdInstall(args)
	case "uninstall":
		err = cmdUninstall(args)
	case "run":
		err = cmdRun(args)
	case "status":
		code, err = cmdStatus(args)
	case "version", "--version", "-v":
		fmt.Printf("axiom-bridge %s (%s/%s, %s)\n", version, runtime.GOOS, runtime.GOARCH, runtime.Version())
	case "help", "-h", "--help":
		fmt.Printf(usage, version)
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n"+usage, cmd, version)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	os.Exit(code)
}

// listFlag is a repeatable, comma-separable flag (--allow).
type listFlag []string

func (l *listFlag) String() string     { return strings.Join(*l, ",") }
func (l *listFlag) Set(v string) error { *l = append(*l, v); return nil }

// common are the flags install and run share; they override config.json.
type common struct {
	code, hub, caFile, dir, logLevel string
	allow                            listFlag
	allowPublic                      bool
}

func (c *common) register(fs *flag.FlagSet) {
	fs.StringVar(&c.code, "code", "", "enrollment code from the hub (AXB-XXXX-XXXX-XXXX)")
	fs.StringVar(&c.hub, "hub", "", "hub bridge URL (default "+config.DefaultHubURL+")")
	fs.Var(&c.allow, "allow", "only these camera IPs or prefixes may be reached (repeatable, comma-separated)")
	fs.BoolVar(&c.allowPublic, "allow-public", false, "permit camera addresses outside RFC 1918")
	fs.StringVar(&c.caFile, "ca-file", "", "extra PEM roots for the hub certificate")
	fs.StringVar(&c.logLevel, "log-level", "", "debug, info, warn or error")
}

// apply puts the flags that were given over the loaded config.
func (c *common) apply(cfg *config.Config) error {
	if c.hub != "" {
		cfg.HubURL = c.hub
	}
	if len(c.allow) > 0 {
		if _, err := policy.ParseAllow(c.allow); err != nil {
			return err
		}
		cfg.Allow = append([]string(nil), c.allow...)
	}
	if c.allowPublic {
		cfg.AllowPublic = true
	}
	if c.caFile != "" {
		cfg.CAFile = c.caFile
	}
	if c.logLevel != "" {
		cfg.LogLevel = c.logLevel
	}
	return cfg.Validate()
}

func paths(dir string) config.Paths {
	if dir != "" {
		return config.PathsIn(dir)
	}
	return config.DefaultPaths()
}

// newRunner wires the bridge from its configuration.
func newRunner(cfg config.Config, p config.Paths, tokens tunnel.TokenStore, log *slog.Logger) (*tunnel.Runner, error) {
	allow, err := policy.ParseAllow(cfg.Allow)
	if err != nil {
		return nil, err
	}
	return &tunnel.Runner{
		Config:     cfg,
		Tokens:     tokens,
		Policy:     policy.New(policy.Options{AllowPublic: cfg.AllowPublic, Allow: allow}),
		Log:        log,
		Version:    version,
		StatusPath: p.Status,
	}, nil
}

// doEnroll spends the code, then connects once to show the bridge's name and Site from the welcome.
func doEnroll(ctx context.Context, cfg config.Config, code string, tokens tunnel.TokenStore, out io.Writer) error {
	client, err := httpClientFor(cfg)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Enrolling with %s …\n", cfg.HubURL)
	id, err := enroll.Enroll(ctx, client, cfg.HubURL, code, version, tokens)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Enrolled: bridge %s\n", id)
	verify(ctx, cfg, tokens, client, out)
	return nil
}

func httpClientFor(cfg config.Config) (*http.Client, error) { return tunnel.HTTPClient(cfg) }

// verify prints who the hub says this bridge is; a failure is only a warning (the service keeps retrying).
func verify(ctx context.Context, cfg config.Config, tokens tunnel.TokenStore, client *http.Client, out io.Writer) {
	r := &tunnel.Runner{Config: cfg, Tokens: tokens, Version: version, HTTPClient: client}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	w, err := r.Verify(ctx)
	if err != nil {
		fmt.Fprintf(out, "Warning: could not confirm the enrollment with the hub yet (%v); the service will keep trying.\n", err)
		return
	}
	site := w.SiteName()
	if site == "" {
		site = "(none)"
	}
	fmt.Fprintf(out, "Bridge: %s   Site: %s   (id %s)\n", w.Name, site, w.BridgeID)
}

func cmdRun(args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	var c common
	c.register(fs)
	fs.StringVar(&c.dir, "dir", "", "keep config, token, status and logs in this directory")
	_ = fs.Parse(args)
	p := paths(c.dir)
	service := isService()
	cfg, err := config.Load(p.Config)
	if err != nil {
		return err
	}
	if err := c.apply(&cfg); err != nil {
		return err
	}
	tokens, err := tokenStore(p, service)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if c.code != "" {
		if err := config.Save(p.Config, cfg); err != nil { // the enrolled bridge keeps these settings
			return err
		}
		if err := doEnroll(ctx, cfg, c.code, tokens, os.Stdout); err != nil {
			return err
		}
	}
	log, closer, err := logx.Open(p.LogDir, cfg.LogLevel, !service)
	if err != nil {
		return err
	}
	defer closer.Close()
	r, err := newRunner(cfg, p, tokens, log)
	if err != nil {
		return err
	}
	if service {
		return runService(r.Run)
	}
	return r.Run(ctx)
}

func cmdStatus(args []string) (int, error) {
	fs := flag.NewFlagSet("status", flag.ExitOnError)
	dir := fs.String("dir", "", "the directory given to run --dir")
	_ = fs.Parse(args)
	p := paths(*dir)
	if st, err := serviceState(); err == nil && st != "" {
		fmt.Printf("Service:     %s\n", st)
	}
	st, err := config.ReadStatus(p.Status)
	if errors.Is(err, os.ErrNotExist) {
		fmt.Println("Connection:  no status yet (the bridge has not run on this PC)")
		return 1, nil
	}
	if err != nil {
		return 1, err
	}
	printStatus(os.Stdout, st, time.Now())
	if st.Connected && time.Since(st.UpdatedAt) < 2*time.Minute {
		return 0, nil
	}
	return 1, nil
}

// printStatus renders status.json for people.
func printStatus(w io.Writer, st config.Status, now time.Time) {
	state := st.State
	if st.Connected {
		state = "connected"
	}
	if st.State != "stopped" && now.Sub(st.UpdatedAt) > 2*time.Minute {
		state += fmt.Sprintf(" (stale: last update %s ago; is the bridge running?)", ago(now, st.UpdatedAt))
	}
	fmt.Fprintf(w, "Connection:  %s", state)
	if !st.Since.IsZero() {
		fmt.Fprintf(w, " since %s (%s ago)", st.Since.Local().Format("2006-01-02 15:04:05"), ago(now, st.Since))
	}
	fmt.Fprintln(w)
	fmt.Fprintf(w, "Hub:         %s\n", st.Hub)
	if st.Name != "" || st.BridgeID != "" {
		fmt.Fprintf(w, "Bridge:      %s (%s)   Site: %s\n", st.Name, st.BridgeID, st.Site)
	}
	fmt.Fprintf(w, "Streams:     %d open   Targets: %d (rev %d)\n", st.Streams, st.Targets, st.TargetsRev)
	fmt.Fprintf(w, "Traffic:     %s to the hub, %s to cameras\n", human(st.BytesUp), human(st.BytesDown))
	if st.LastError != "" {
		fmt.Fprintf(w, "Last error:  %s (%s ago)\n", st.LastError, ago(now, st.LastErrorAt))
	}
	if !st.NextRetry.IsZero() && !st.Connected && st.NextRetry.After(now) {
		fmt.Fprintf(w, "Next try:    in %s\n", st.NextRetry.Sub(now).Round(time.Second))
	}
	for k, v := range st.TargetErrors {
		fmt.Fprintf(w, "Camera:      %s: %s\n", k, v)
	}
	fmt.Fprintf(w, "Version:     %s (pid %d)\n", st.Version, st.PID)
}

func ago(now, t time.Time) string {
	if t.IsZero() {
		return "?"
	}
	return now.Sub(t).Round(time.Second).String()
}

func human(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for m := n / unit; m >= unit; m /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), "KMGTPE"[exp])
}
