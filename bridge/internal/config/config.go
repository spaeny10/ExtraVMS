// Package config holds the bridge's files: config.json (hub URL and the owner's local rules), the device token
// (kept in its own file, readable only by SYSTEM and Administrators on Windows, 0600 on Linux) and status.json (what
// `axiom-bridge status` shows). Windows keeps them in %ProgramData%\Axiom Bridge; Linux in /etc/axiom-bridge
// (config, token) and /var/lib/axiom-bridge (status, logs). AXIOM_BRIDGE_DIR puts everything in one directory.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// DefaultHubURL is the production hub's bridge endpoint.
const DefaultHubURL = "wss://hub.axiomvision.ai/bridge"

// Config is config.json. It holds no secrets (the token is a separate file).
type Config struct {
	HubURL      string   `json:"hub_url"`
	Allow       []string `json:"allow,omitempty"`        // owner allow list: IPs or CIDR prefixes the bridge may reach
	AllowPublic bool     `json:"allow_public,omitempty"` // permit non-RFC1918 camera addresses
	CAFile      string   `json:"ca_file,omitempty"`      // extra PEM roots for the hub's certificate (private hubs)
	LogLevel    string   `json:"log_level,omitempty"`    // debug, info (default), warn, error
}

// Paths are where the bridge keeps its files.
type Paths struct {
	Config string
	Token  string
	Status string
	LogDir string
}

// DefaultPaths are the per-OS locations, or all under AXIOM_BRIDGE_DIR when it is set.
func DefaultPaths() Paths {
	if d := os.Getenv("AXIOM_BRIDGE_DIR"); d != "" {
		return PathsIn(d)
	}
	if runtime.GOOS == "windows" {
		pd := os.Getenv("ProgramData")
		if pd == "" {
			pd = `C:\ProgramData`
		}
		return PathsIn(filepath.Join(pd, "Axiom Bridge"))
	}
	return Paths{
		Config: "/etc/axiom-bridge/config.json",
		Token:  "/etc/axiom-bridge/token",
		Status: "/var/lib/axiom-bridge/status.json",
		LogDir: "/var/lib/axiom-bridge/logs",
	}
}

// PathsIn keeps every file in one directory (Windows layout, tests, AXIOM_BRIDGE_DIR).
func PathsIn(dir string) Paths {
	return Paths{
		Config: filepath.Join(dir, "config.json"),
		Token:  filepath.Join(dir, "token"),
		Status: filepath.Join(dir, "status.json"),
		LogDir: filepath.Join(dir, "logs"),
	}
}

// Load reads config.json; a missing file gives the defaults (so `run --code` works on a bare Linux box).
func Load(path string) (Config, error) {
	c := Config{HubURL: DefaultHubURL}
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(b, &c); err != nil {
		return c, fmt.Errorf("%s: %w", path, err)
	}
	if c.HubURL == "" {
		c.HubURL = DefaultHubURL
	}
	return c, c.Validate()
}

// Save writes config.json atomically (ordinary permissions: nothing in it is secret).
func Save(path string, c Config) error {
	if err := c.Validate(); err != nil {
		return err
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return WriteFileAtomic(path, append(b, '\n'), 0o644)
}

// Validate checks the hub URL (wss, or ws only to this machine) and the log level.
func (c Config) Validate() error {
	if err := CheckHubURL(c.HubURL); err != nil {
		return err
	}
	switch strings.ToLower(c.LogLevel) {
	case "", "debug", "info", "warn", "error":
	default:
		return fmt.Errorf("log_level %q: use debug, info, warn or error", c.LogLevel)
	}
	return nil
}

// CheckHubURL requires wss:// so the token and the camera traffic are encrypted; plain ws:// is accepted only for
// a hub on this machine (localhost, 127.0.0.1, ::1), which is what development and the tests use.
func CheckHubURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("hub URL %q: %w", raw, err)
	}
	if u.Host == "" {
		return fmt.Errorf("hub URL %q has no host", raw)
	}
	switch u.Scheme {
	case "wss":
		return nil
	case "ws":
		if IsLocalHost(u.Hostname()) {
			return nil
		}
		return fmt.Errorf("hub URL %q: wss:// is required (ws:// only for localhost)", raw)
	default:
		return fmt.Errorf("hub URL %q: scheme must be wss://", raw)
	}
}

// IsLocalHost: the names and addresses that mean this machine.
func IsLocalHost(h string) bool {
	if strings.EqualFold(h, "localhost") {
		return true
	}
	ip := net.ParseIP(h)
	return ip != nil && (ip.Equal(net.IPv4(127, 0, 0, 1)) || ip.Equal(net.IPv6loopback))
}

// WriteFileAtomic writes to a temporary file in the same directory and renames it over path, so a crash never
// leaves a half-written file behind.
func WriteFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	ok := false
	defer func() {
		if !ok {
			_ = f.Close()
			_ = os.Remove(tmp)
		}
	}()
	if _, err := f.Write(data); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(tmp, perm); err != nil {
			return err
		}
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	ok = true
	return nil
}
