//go:build !windows

package main

import (
	"context"
	"errors"

	"axiomvision.ai/bridge/internal/config"
)

func isService() bool { return false }

func runService(run func(context.Context) error) error { return run(context.Background()) }

func serviceState() (string, error) { return "", nil }

// tokenStore: the token file is 0600, owned by whoever runs the bridge (root under systemd).
func tokenStore(p config.Paths, _ bool) (config.TokenFile, error) {
	return config.TokenFile{Path: p.Token}, nil
}

var errNotWindows = errors.New("install/uninstall manage the Windows service; on Linux run `axiom-bridge run --code …` once, then under systemd (see README.md)")

func cmdInstall([]string) error   { return errNotWindows }
func cmdUninstall([]string) error { return errNotWindows }
