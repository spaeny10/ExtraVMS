package logx

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRotatorKeepsThreeFiles(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRotator(filepath.Join(dir, "bridge.log"))
	if err != nil {
		t.Fatal(err)
	}
	r.MaxBytes = 100
	line := []byte(strings.Repeat("x", 39) + "\n") // 40 bytes: two fit in a file
	for i := 0; i < 20; i++ {
		if _, err := r.Write(line); err != nil {
			t.Fatal(err)
		}
	}
	_ = r.Close()
	files, _ := filepath.Glob(filepath.Join(dir, "bridge.log*"))
	if len(files) != 3 {
		t.Fatalf("files %v, want 3", files)
	}
	for _, f := range files {
		st, _ := os.Stat(f)
		if st.Size() > 100 {
			t.Errorf("%s is %d bytes", f, st.Size())
		}
	}
	// reopening appends to the existing file
	r2, _ := NewRotator(filepath.Join(dir, "bridge.log"))
	if r2.size == 0 {
		t.Error("size not picked up on reopen")
	}
	_ = r2.Close()
}

func TestLevelsAndRedact(t *testing.T) {
	var b bytes.Buffer
	l := New(&b, "warn")
	l.Info("hidden")
	l.Warn("shown")
	if strings.Contains(b.String(), "hidden") || !strings.Contains(b.String(), "shown") {
		t.Fatal(b.String())
	}
	if Redact("AXB-ABCD-EFGH") != "AXB-…" || Redact("ab") != "****" {
		t.Fatal(Redact("AXB-ABCD-EFGH"))
	}
}
