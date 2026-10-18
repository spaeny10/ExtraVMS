// Package logx is the bridge's logging: log/slog text lines into a size-rotated file (5 MB × 3 files, so a PC that
// runs for years never fills its disk), optionally mirrored to stderr for a foreground `run`. Callers never log
// tokens, enrollment codes or stream payloads; Redact exists for the few places that must mention a credential.
package logx

import (
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

const (
	MaxBytes = 5 << 20 // one log file
	Keep     = 3       // files in all: bridge.log, bridge.log.1, bridge.log.2
)

// Rotator is an io.Writer appending to a file and rotating it once it reaches MaxBytes.
type Rotator struct {
	Path     string
	MaxBytes int64
	Keep     int

	mu   sync.Mutex
	f    *os.File
	size int64
}

// NewRotator opens (or creates) path for appending.
func NewRotator(path string) (*Rotator, error) {
	r := &Rotator{Path: path, MaxBytes: MaxBytes, Keep: Keep}
	if err := r.open(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *Rotator) open() error {
	if err := os.MkdirAll(filepath.Dir(r.Path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(r.Path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		_ = f.Close()
		return err
	}
	r.f, r.size = f, st.Size()
	return nil
}

// Write appends p, rotating first when p would take the file past MaxBytes.
func (r *Rotator) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		if err := r.open(); err != nil {
			return 0, err
		}
	}
	if r.size > 0 && r.size+int64(len(p)) > r.MaxBytes {
		if err := r.rotate(); err != nil {
			return 0, err
		}
	}
	n, err := r.f.Write(p)
	r.size += int64(n)
	return n, err
}

// rotate shifts bridge.log.N-1 → .N … bridge.log → .1, dropping the oldest, and starts a new file.
func (r *Rotator) rotate() error {
	_ = r.f.Close()
	r.f = nil
	keep := r.Keep
	if keep < 1 {
		keep = 1
	}
	_ = os.Remove(fmt.Sprintf("%s.%d", r.Path, keep-1))
	for i := keep - 2; i >= 1; i-- {
		_ = os.Rename(fmt.Sprintf("%s.%d", r.Path, i), fmt.Sprintf("%s.%d", r.Path, i+1))
	}
	if keep > 1 {
		_ = os.Rename(r.Path, r.Path+".1")
	} else {
		_ = os.Remove(r.Path)
	}
	return r.open()
}

// Close closes the current file.
func (r *Rotator) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		return nil
	}
	err := r.f.Close()
	r.f = nil
	return err
}

// ParseLevel maps config.json's log_level to a slog level (info when empty or unknown).
func ParseLevel(s string) slog.Level {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	}
	return slog.LevelInfo
}

// New returns a text logger writing to w at the given level.
func New(w io.Writer, level string) *slog.Logger {
	return slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: ParseLevel(level)}))
}

// Open is the bridge's logger: dir/bridge.log (rotated) and, when echo is set, stderr too. The returned closer
// closes the file.
func Open(dir, level string, echo bool) (*slog.Logger, io.Closer, error) {
	r, err := NewRotator(filepath.Join(dir, "bridge.log"))
	if err != nil {
		return nil, nil, err
	}
	var w io.Writer = r
	if echo {
		w = io.MultiWriter(r, os.Stderr)
	}
	return New(w, level), r, nil
}

// Discard is a logger that drops everything (tests that do not look at logs).
func Discard() *slog.Logger { return slog.New(slog.DiscardHandler) }

// Redact shows only enough of a credential to tell two apart: its first 4 characters.
func Redact(s string) string {
	if len(s) <= 4 {
		return "****"
	}
	return s[:4] + "…"
}
