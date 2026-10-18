package config

import (
	"encoding/json"
	"os"
	"time"
)

// Status is status.json, rewritten by the running bridge on every state change and heartbeat so `axiom-bridge
// status` (another process, maybe unelevated) can show the last connection without talking to the service.
type Status struct {
	Version      string            `json:"version"`
	PID          int               `json:"pid"`
	Hub          string            `json:"hub"`
	State        string            `json:"state"` // connecting, connected, waiting, not_enrolled, revoked, stopped
	Connected    bool              `json:"connected"`
	Since        time.Time         `json:"since,omitzero"` // when the current state began
	BridgeID     string            `json:"bridge_id,omitempty"`
	Name         string            `json:"name,omitempty"`
	Site         string            `json:"site,omitempty"`
	LastError    string            `json:"last_error,omitempty"`
	LastErrorAt  time.Time         `json:"last_error_at,omitzero"`
	NextRetry    time.Time         `json:"next_retry,omitzero"`
	Streams      int               `json:"streams"`
	Targets      int               `json:"targets"`
	TargetsRev   int64             `json:"targets_rev"`
	BytesUp      int64             `json:"bytes_up"`
	BytesDown    int64             `json:"bytes_down"`
	TargetErrors map[string]string `json:"target_errors,omitempty"`
	UpdatedAt    time.Time         `json:"updated_at"`
}

// WriteStatus replaces status.json atomically (readable by everyone: it holds no secrets).
func WriteStatus(path string, s Status) error {
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return WriteFileAtomic(path, append(b, '\n'), 0o644)
}

// ReadStatus reads status.json.
func ReadStatus(path string) (Status, error) {
	var s Status
	b, err := os.ReadFile(path)
	if err != nil {
		return s, err
	}
	return s, json.Unmarshal(b, &s)
}
