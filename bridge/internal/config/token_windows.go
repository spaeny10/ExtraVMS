//go:build windows

package config

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// writeSecret creates a temporary file with the given DACL already applied (CreateFile with security attributes, so
// the token is never readable by others, not even for a moment), writes it, then moves it over path.
func writeSecret(path string, data []byte, sddl string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return fmt.Errorf("token DACL: %w", err)
	}
	sa := windows.SecurityAttributes{SecurityDescriptor: sd}
	sa.Length = uint32(unsafe.Sizeof(sa))
	tmp := filepath.Join(dir, fmt.Sprintf(".%s.%d.%d", filepath.Base(path), os.Getpid(), time.Now().UnixNano()))
	p, err := windows.UTF16PtrFromString(tmp)
	if err != nil {
		return err
	}
	// DELETE on our own handle lets us rename it below even though the DACL we just set may not grant it to us
	h, err := windows.CreateFile(p, windows.GENERIC_WRITE|windows.DELETE, 0, &sa, windows.CREATE_NEW, windows.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return fmt.Errorf("create %s: %w", tmp, err)
	}
	f := os.NewFile(uintptr(h), tmp)
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
	if err := renameHandle(windows.Handle(f.Fd()), path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	ok = true
	return f.Close()
}

// fileRenameInfo is FILE_RENAME_INFO (the BOOLEAN ReplaceIfExists shares its union with a DWORD of flags).
type fileRenameInfo struct {
	ReplaceIfExists uint32
	RootDirectory   windows.Handle
	FileNameLength  uint32
	FileName        [1]uint16
}

// renameHandle moves an open file over path through its own handle (SetFileInformationByHandle FileRenameInfo), so
// the rename needs only the DELETE access the handle was opened with, not access granted by the new file's DACL.
func renameHandle(h windows.Handle, path string) error {
	name, err := windows.UTF16FromString(path)
	if err != nil {
		return err
	}
	var hdr fileRenameInfo
	off := int(unsafe.Offsetof(hdr.FileName))
	buf := make([]byte, off+len(name)*2)
	info := (*fileRenameInfo)(unsafe.Pointer(&buf[0]))
	info.ReplaceIfExists = 1
	info.FileNameLength = uint32((len(name) - 1) * 2) // bytes, without the terminating NUL
	copy(unsafe.Slice((*uint16)(unsafe.Pointer(&buf[off])), len(name)), name)
	return windows.SetFileInformationByHandle(h, windows.FileRenameInfo, &buf[0], uint32(len(buf)))
}

// UserTokenSDDL is DefaultTokenSDDL plus full control for the user running this process, for a foreground `run`
// from an unelevated prompt (whose Administrators membership is deny-only) and for tests.
func UserTokenSDDL() (string, error) {
	u, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return "", err
	}
	return DefaultTokenSDDL + "(A;;FA;;;" + u.User.Sid.String() + ")", nil
}

// FileSDDL reads a file's DACL back as SDDL (tests, and `status` diagnostics).
func FileSDDL(path string) (string, error) {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return "", err
	}
	return sd.String(), nil
}
