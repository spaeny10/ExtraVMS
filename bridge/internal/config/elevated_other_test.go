//go:build !windows

package config

import (
	"os"
	"testing"
)

func elevated() bool { return os.Geteuid() == 0 }

func unlock(*testing.T, string) {}
