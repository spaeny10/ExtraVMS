//go:build windows

package config

import (
	"testing"

	"golang.org/x/sys/windows"
)

func elevated() bool { return windows.GetCurrentProcessToken().IsElevated() }

// unlock gives the test user back control of a SYSTEM+Administrators token file (the owner may always rewrite the
// DACL) so t.TempDir can remove it.
func unlock(t *testing.T, path string) {
	t.Cleanup(func() {
		sddl, err := UserTokenSDDL()
		if err != nil {
			t.Log(err)
			return
		}
		sd, err := windows.SecurityDescriptorFromString(sddl)
		if err != nil {
			t.Log(err)
			return
		}
		dacl, _, err := sd.DACL()
		if err != nil {
			t.Log(err)
			return
		}
		if err := windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION, nil, nil, dacl, nil); err != nil {
			t.Log("unlock:", err)
		}
	})
}
