package config

import (
	"errors"
	"os"
	"strings"
)

// DefaultTokenSDDL is the Windows DACL of the token file: protected (nothing inherited from %ProgramData%, which
// lets Users read), full control for SYSTEM (the service) and Administrators only.
const DefaultTokenSDDL = "D:P(A;;FA;;;SY)(A;;FA;;;BA)"

// TokenFile is the bridge's device token on disk.
type TokenFile struct {
	Path string
	// SDDL replaces DefaultTokenSDDL on Windows (UserTokenSDDL adds the current user, for a foreground `run` from an
	// unelevated prompt and for tests). Ignored elsewhere: the file is always 0600.
	SDDL string
}

// Load returns the token, or "" when there is none yet.
func (t TokenFile) Load() (string, error) {
	b, err := os.ReadFile(t.Path)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// Save replaces the token atomically; the new file has its restricted permissions before any byte is written.
func (t TokenFile) Save(token string) error {
	if strings.TrimSpace(token) == "" {
		return errors.New("empty token")
	}
	sddl := t.SDDL
	if sddl == "" {
		sddl = DefaultTokenSDDL
	}
	return writeSecret(t.Path, []byte(strings.TrimSpace(token)+"\n"), sddl)
}

// Remove deletes the token (after `revoked`, or uninstall --purge); a missing file is not an error.
func (t TokenFile) Remove() error {
	err := os.Remove(t.Path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}
