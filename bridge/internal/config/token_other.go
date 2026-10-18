//go:build !windows

package config

import (
	"fmt"
	"os"
	"path/filepath"
)

// writeSecret creates the temporary file 0600 from the start (O_EXCL, so nothing else can have it open), writes it,
// then renames it over path.
func writeSecret(path string, data []byte, _ string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp := filepath.Join(dir, fmt.Sprintf(".%s.%d", filepath.Base(path), os.Getpid()))
	_ = os.Remove(tmp)
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
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
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	ok = true
	return nil
}

// UserTokenSDDL has no meaning outside Windows (the token file is 0600, readable by its owner).
func UserTokenSDDL() (string, error) { return "", nil }

// FileSDDL reports the file mode where Windows would report a DACL.
func FileSDDL(path string) (string, error) {
	st, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	return st.Mode().Perm().String(), nil
}
