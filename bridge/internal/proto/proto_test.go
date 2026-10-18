package proto

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// golden is testdata/golden.json, written by gen_golden.py from Python's tunnelproto.
type golden struct {
	ChunkSize int               `json:"chunk_size"`
	ToHub     map[string]string `json:"to_hub"`
	FromHub   map[string]string `json:"from_hub"`
	Chunks    []struct {
		ID    uint32 `json:"id"`
		Data  string `json:"data"`
		Frame string `json:"frame"`
	} `json:"chunks"`
	Split struct {
		ID     uint32 `json:"id"`
		Len    int    `json:"len"`
		Mod    int    `json:"mod"`
		Frames []struct {
			Len    int    `json:"len"`
			SHA256 string `json:"sha256"`
		} `json:"frames"`
	} `json:"split"`
}

func load(t *testing.T) golden {
	t.Helper()
	b, err := os.ReadFile("testdata/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(b, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func str(s string) *string { return &s }

func TestEncodeMatchesPython(t *testing.T) {
	g := load(t)
	if g.ChunkSize != Chunk {
		t.Fatalf("CHUNK: python %d, go %d", g.ChunkSize, Chunk)
	}
	cases := map[string]any{
		"hello": Hello{T: "hello", Proto: 1, Version: "0.1.0", OS: "windows", Arch: "amd64", Hostname: "Büro-PC",
			LAN: []LANAddr{{If: "Ethernet", Addr: "192.168.1.20/24"}}, Caps: []string{"tcp", "probe"}},
		"heartbeat": Heartbeat{T: "heartbeat", Streams: 2, BytesUp: 123456789, BytesDown: 4321, UptimeS: 3600,
			TargetErrors: map[string]string{"bt_x:554": "connection refused"}},
		"heartbeat_empty": Heartbeat{T: "heartbeat", UptimeS: 5, TargetErrors: map[string]string{}},
		"opened":          Opened{T: "opened", ID: 1},
		"end":             End{T: "end", ID: 3},
		"abort":           Abort{T: "abort", ID: 5, Reason: "policy: <loopback> & co"},
		"credit":          Credit{T: "credit", ID: 4294967295, Bytes: 65536},
		"probe_ok":        ProbeResult{T: "probe_result", ID: json.RawMessage("7"), OK: true, MS: 12},
		"probe_fail":      ProbeResult{T: "probe_result", ID: json.RawMessage(`"p1"`), MS: 5000, Error: str("timeout")},
		"rotated":         Rotated{T: "rotated"},
		"pong":            Pong{T: "pong", TS: json.RawMessage("1760000000.25")},
		"pong_bare":       Pong{T: "pong"},
	}
	for name, v := range cases {
		want, ok := g.ToHub[name]
		if !ok {
			t.Errorf("%s: no golden vector", name)
			continue
		}
		got, err := Encode(v)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("%s:\n got %s\nwant %s", name, got, want)
		}
	}
	if len(cases) != len(g.ToHub) {
		t.Errorf("golden has %d bridge→hub vectors, test covers %d", len(g.ToHub), len(cases))
	}
}

func TestDecodePythonFrames(t *testing.T) {
	g := load(t)
	frame := func(name string) []byte { return []byte(g.FromHub[name]) }
	typ := func(name, want string) {
		t.Helper()
		got, err := TypeOf(frame(name))
		if err != nil || got != want {
			t.Fatalf("%s: type %q %v, want %q", name, got, err, want)
		}
	}
	check := func(name string, v, want any) {
		t.Helper()
		if err := Decode(frame(name), v); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if !reflect.DeepEqual(reflect.ValueOf(v).Elem().Interface(), want) {
			t.Fatalf("%s: got %+v want %+v", name, reflect.ValueOf(v).Elem().Interface(), want)
		}
	}

	typ("welcome", "welcome")
	var w Welcome
	if err := Decode(frame("welcome"), &w); err != nil {
		t.Fatal(err)
	}
	if w.BridgeID != "br_abc" || w.Name != "Gate PC" || w.HeartbeatS != 30 || w.MaxStreams != 16 || w.Window != 524288 || w.SiteName() != "Ranch" {
		t.Fatalf("welcome: %+v site %q", w, w.SiteName())
	}
	var w2 Welcome
	if err := Decode(frame("welcome_site_str"), &w2); err != nil || w2.SiteName() != "Ranch Büro" || w2.Window != 0 {
		t.Fatalf("welcome (site string): %+v %v", w2, err)
	}
	typ("targets", "targets")
	check("targets", &Targets{}, Targets{Rev: 3, Items: []Target{{ID: "bt_1", Host: "192.168.1.50", Ports: []int{554, 80, 555}}}})
	typ("open", "open")
	check("open", &Open{}, Open{ID: 1, Target: "bt_1", Host: "192.168.1.50", Port: 554})
	check("probe", &Probe{}, Probe{ID: json.RawMessage("9"), Target: "bt_1", Host: "192.168.1.50", Port: 80})
	check("rotate", &Rotate{}, Rotate{Token: "tok_new"})
	check("revoked", &Revoked{}, Revoked{Reason: "retired by admin"})
	check("enrolled", &Enrolled{}, Enrolled{BridgeID: "br_abc", Token: "tok_1"})
	check("end", &StreamRef{}, StreamRef{ID: 3})
	check("abort", &StreamRef{}, StreamRef{ID: 5, Reason: "host closed"})
	check("credit", &StreamRef{}, StreamRef{ID: 7, Bytes: 65536})
	check("ping", &Ping{}, Ping{T: "ping", TS: json.RawMessage("1760000000.5")})
	typ("unknown", "turn")

	if _, err := TypeOf([]byte("not json")); err == nil {
		t.Fatal("TypeOf accepted garbage")
	}
}

func TestChunkMatchesPython(t *testing.T) {
	g := load(t)
	for _, c := range g.Chunks {
		data, _ := hex.DecodeString(c.Data)
		want, _ := hex.DecodeString(c.Frame)
		got := ChunkFrame(c.ID, data)
		if hex.EncodeToString(got) != c.Frame {
			t.Errorf("chunk %d: got %x want %x", c.ID, got, want)
		}
		id, payload, err := ParseChunk(want)
		if err != nil || id != c.ID || hex.EncodeToString(payload) != c.Data {
			t.Errorf("parse %d: %d %x %v", c.ID, id, payload, err)
		}
	}
	if _, _, err := ParseChunk([]byte{1, 2, 3}); err == nil {
		t.Error("ParseChunk accepted 3 bytes")
	}
}

// TestSplitMatchesPython: a 150 000-byte body cut into Chunk-sized frames gives Python's frames exactly.
func TestSplitMatchesPython(t *testing.T) {
	g := load(t)
	data := make([]byte, g.Split.Len)
	for i := range data {
		data[i] = byte(i % g.Split.Mod)
	}
	var frames [][]byte
	for i := 0; i < len(data); i += Chunk {
		frames = append(frames, ChunkFrame(g.Split.ID, data[i:min(i+Chunk, len(data))]))
	}
	if len(frames) != len(g.Split.Frames) {
		t.Fatalf("%d frames, python %d", len(frames), len(g.Split.Frames))
	}
	for i, f := range frames {
		sum := sha256.Sum256(f)
		if len(f) != g.Split.Frames[i].Len || hex.EncodeToString(sum[:]) != g.Split.Frames[i].SHA256 {
			t.Errorf("frame %d differs (len %d, python %d)", i, len(f), g.Split.Frames[i].Len)
		}
	}
}
