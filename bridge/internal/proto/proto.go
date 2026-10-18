// Package proto is the Axiom Bridge side of the tunnelproto framing (tunnelproto/tunnelproto/__init__.py) as used on
// the hub's WS /bridge endpoint (docs/axiom-bridge-plan.md, "Wire protocol"; binding for the hub and the bridge).
//
// Text frames are JSON control messages with a type in "t"; unknown types and fields are ignored. Binary frames are
// a 4-byte big-endian stream id followed by at most Chunk bytes of the stream. The hub opens streams (odd ids); the
// bridge answers `opened` or `abort`, then both sides carry chunks, `end` (TCP half-close) and `credit` (bytes the
// receiver has written to its TCP socket, so the sender may send that many more).
package proto

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
)

const (
	Version           = 1            // hello.proto
	Chunk             = 64 * 1024    // largest payload of one binary frame (tunnelproto.CHUNK)
	HeaderLen         = 4            // stream id prefix of a binary frame
	MaxBinary         = Chunk + 1024 // read limit for a binary frame: a little slack over HeaderLen + Chunk
	MaxText           = 1 << 20      // read limit for a control frame
	DefaultWindow     = 512 * 1024   // per-stream window until the hub's welcome says otherwise
	DefaultMaxStreams = 16           // concurrent streams until the welcome says otherwise
	DefaultHeartbeatS = 30           // heartbeat period until the welcome says otherwise
)

// Encode returns a control frame as tunnelproto.encode writes it: compact JSON, non-ASCII left as UTF-8 and no HTML
// escaping (json.dumps(frame, separators=(",", ":"), ensure_ascii=False)), so golden vectors match byte for byte.
func Encode(v any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(b.Bytes(), []byte("\n")), nil
}

// ChunkFrame is tunnelproto.chunk: the stream id, big-endian, then the bytes.
func ChunkFrame(id uint32, data []byte) []byte {
	b := make([]byte, HeaderLen+len(data))
	binary.BigEndian.PutUint32(b, id)
	copy(b[HeaderLen:], data)
	return b
}

// ErrShortChunk is a binary frame too short to hold a stream id.
var ErrShortChunk = errors.New("binary frame shorter than its stream id")

// ParseChunk splits a binary frame into its stream id and payload (the payload aliases b).
func ParseChunk(b []byte) (uint32, []byte, error) {
	if len(b) < HeaderLen {
		return 0, nil, ErrShortChunk
	}
	return binary.BigEndian.Uint32(b), b[HeaderLen:], nil
}

// TypeOf reads just the "t" of a control frame, so the caller can pick the struct to decode it into.
func TypeOf(b []byte) (string, error) {
	var h struct {
		T string `json:"t"`
	}
	if err := json.Unmarshal(b, &h); err != nil {
		return "", fmt.Errorf("control frame is not a JSON object: %w", err)
	}
	return h.T, nil
}

// Decode fills v (one of the hub→bridge frame structs) from a control frame; unknown fields are ignored.
func Decode(b []byte, v any) error { return json.Unmarshal(b, v) }

// ---- bridge → hub

// LANAddr is one of the bridge PC's own IPv4 addresses, shown on the hub so an admin can see which LAN it sits on.
type LANAddr struct {
	If   string `json:"if"`
	Addr string `json:"addr"` // address with prefix length, e.g. "192.168.1.20/24"
}

// Hello is the first frame on every connection (and the only one before `enrolled` on an Enroll connection).
type Hello struct {
	T        string    `json:"t"`
	Proto    int       `json:"proto"`
	Version  string    `json:"version"`
	OS       string    `json:"os"`
	Arch     string    `json:"arch"`
	Hostname string    `json:"hostname"`
	LAN      []LANAddr `json:"lan"`
	Caps     []string  `json:"caps"`
}

// Heartbeat reports counters every heartbeat_s; it may jump ahead of queued stream data.
type Heartbeat struct {
	T            string            `json:"t"`
	Streams      int               `json:"streams"`
	BytesUp      int64             `json:"bytes_up"`   // camera → hub, since the bridge started
	BytesDown    int64             `json:"bytes_down"` // hub → camera, since the bridge started
	UptimeS      int64             `json:"uptime_s"`
	TargetErrors map[string]string `json:"target_errors"` // "bt_x:554" → last connect error, cleared on success
}

// Opened answers an `open` once the TCP connection to the camera is up.
type Opened struct {
	T  string `json:"t"`
	ID uint32 `json:"id"`
}

// End is a half-close: the sender will send no more bytes on the stream (TCP FIN). Both directions use it.
type End struct {
	T  string `json:"t"`
	ID uint32 `json:"id"`
}

// Abort resets a stream (TCP RST). Both directions use it; the reason is for logs and the hub's UI.
type Abort struct {
	T      string `json:"t"`
	ID     uint32 `json:"id"`
	Reason string `json:"reason"`
}

// Credit returns window: the receiver has written these bytes to its TCP socket. Both directions use it.
type Credit struct {
	T     string `json:"t"`
	ID    uint32 `json:"id"`
	Bytes int64  `json:"bytes"`
}

// ProbeResult answers a `probe` (a TCP connect test from the hub's "Test" button). Error is null when OK.
type ProbeResult struct {
	T     string          `json:"t"`
	ID    json.RawMessage `json:"id"` // echoed exactly as the hub sent it
	OK    bool            `json:"ok"`
	MS    int64           `json:"ms"`
	Error *string         `json:"error"`
}

// Rotated confirms a `rotate`: the new token is on disk and will be used from the next connection.
type Rotated struct {
	T string `json:"t"`
}

// Ping / Pong are the JSON keepalives tunnelproto peers exchange; ts is echoed untouched.
type Ping struct {
	T  string          `json:"t"`
	TS json.RawMessage `json:"ts,omitempty"`
}

// Pong answers a Ping.
type Pong struct {
	T  string          `json:"t"`
	TS json.RawMessage `json:"ts,omitempty"`
}

// ---- hub → bridge

// Welcome follows a Bearer handshake: who the bridge is and the limits it must keep.
type Welcome struct {
	BridgeID   string          `json:"bridge_id"`
	Name       string          `json:"name"`
	Site       json.RawMessage `json:"site"` // a name, or an object with "name" (SiteName reads either)
	HeartbeatS int             `json:"heartbeat_s"`
	MaxStreams int             `json:"max_streams"`
	Window     int64           `json:"window"`
}

// SiteName is the Site's display name whether the hub sent a string or an object.
func (w Welcome) SiteName() string {
	if len(w.Site) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(w.Site, &s) == nil {
		return s
	}
	var o struct {
		Name string `json:"name"`
		ID   string `json:"id"`
	}
	if json.Unmarshal(w.Site, &o) == nil {
		if o.Name != "" {
			return o.Name
		}
		return o.ID
	}
	return ""
}

// Target is one camera the hub assigned to this bridge: the address and the ports it may be reached on.
type Target struct {
	ID    string `json:"id"`
	Host  string `json:"host"`
	Ports []int  `json:"ports"`
}

// Targets is the full list of assignments; each one replaces the previous list.
type Targets struct {
	Rev   int64    `json:"rev"`
	Items []Target `json:"items"`
}

// Open asks the bridge to connect a new stream to host:port of one of its targets.
type Open struct {
	ID     uint32 `json:"id"`
	Target string `json:"target"`
	Host   string `json:"host"`
	Port   int    `json:"port"`
}

// Probe asks for a TCP connect test (no stream).
type Probe struct {
	ID     json.RawMessage `json:"id"`
	Target string          `json:"target"`
	Host   string          `json:"host"`
	Port   int             `json:"port"`
}

// Rotate hands the bridge a new device token.
type Rotate struct {
	Token string `json:"token"`
}

// Revoked says the bridge was retired on the hub; its token no longer works.
type Revoked struct {
	Reason string `json:"reason"`
}

// Enrolled is the only frame on an Enroll connection: the bridge's id and device token. The hub then closes.
type Enrolled struct {
	BridgeID string `json:"bridge_id"`
	Token    string `json:"token"`
}

// StreamRef is the "id" of end / abort / credit frames from the hub, plus their optional fields.
type StreamRef struct {
	ID     uint32 `json:"id"`
	Bytes  int64  `json:"bytes"`
	Reason string `json:"reason"`
}
