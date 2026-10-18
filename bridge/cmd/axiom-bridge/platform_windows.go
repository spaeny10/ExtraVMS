//go:build windows

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"axiomvision.ai/bridge/internal/config"
	"axiomvision.ai/bridge/internal/winsvc"
)

func isService() bool { return winsvc.IsService() }

func runService(run func(context.Context) error) error { return winsvc.Run(run) }

func serviceState() (string, error) { return winsvc.QueryState(winsvc.ServiceName) }

// tokenStore: the service (and an elevated prompt) use the SYSTEM + Administrators DACL; a foreground run from a
// normal prompt adds the current user, or it could not read back the token it just wrote.
func tokenStore(p config.Paths, service bool) (config.TokenFile, error) {
	tf := config.TokenFile{Path: p.Token}
	if !service && !winsvc.IsElevated() {
		sddl, err := config.UserTokenSDDL()
		if err != nil {
			return tf, err
		}
		tf.SDDL = sddl
	}
	return tf, nil
}

var errElevation = errors.New(`this needs an elevated prompt: right-click PowerShell, "Run as administrator", and run the command again`)

// cmdInstall: copy to Program Files, write config, enroll (printing the bridge and Site), register and start the
// service. Without --code an existing enrollment is kept (an upgrade).
func cmdInstall(args []string) error {
	fs := flag.NewFlagSet("install", flag.ExitOnError)
	var c common
	c.register(fs)
	_ = fs.Parse(args)
	if !winsvc.IsElevated() {
		return errElevation
	}
	p := config.DefaultPaths()
	tokens := config.TokenFile{Path: p.Token}
	if c.code == "" {
		if tok, _ := tokens.Load(); tok == "" {
			return errors.New("--code is required: create a Bridge enrollment code for this Site on the hub")
		}
	}
	cfg, err := config.Load(p.Config)
	if err != nil {
		return err
	}
	if err := c.apply(&cfg); err != nil {
		return err
	}

	fmt.Println("Stopping the existing service, if any …")
	if err := winsvc.Stop(15 * time.Second); err != nil {
		return err
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	exe, err := winsvc.CopySelf(self, winsvc.InstallDir())
	if err != nil {
		return err
	}
	fmt.Printf("Installed:   %s\n", exe)
	if err := config.Save(p.Config, cfg); err != nil {
		return err
	}
	fmt.Printf("Config:      %s\n", p.Config)

	ctx := context.Background()
	if c.code != "" {
		if err := doEnroll(ctx, cfg, c.code, tokens, os.Stdout); err != nil {
			return fmt.Errorf("enrollment failed, service not installed: %w", err)
		}
	} else {
		fmt.Println("Keeping the existing enrollment.")
		client, err := httpClientFor(cfg)
		if err != nil {
			return err
		}
		verify(ctx, cfg, tokens, client, os.Stdout)
	}

	if err := winsvc.Register(exe); err != nil {
		return err
	}
	if err := winsvc.Start(30 * time.Second); err != nil {
		return err
	}
	fmt.Printf("Service %s is running (automatic start, restarts on failure).\n", winsvc.ServiceName)
	fmt.Printf("Logs:        %s\n", filepath.Join(p.LogDir, "bridge.log"))
	fmt.Println(`Check it:    & "` + exe + `" status`)
	return nil
}

// cmdUninstall stops and removes the service and the program; --purge also deletes config, token and logs.
func cmdUninstall(args []string) error {
	fs := flag.NewFlagSet("uninstall", flag.ExitOnError)
	purge := fs.Bool("purge", false, "also delete the config, token and logs")
	_ = fs.Parse(args)
	if !winsvc.IsElevated() {
		return errElevation
	}
	if err := winsvc.Unregister(); err != nil {
		return err
	}
	fmt.Printf("Service %s removed.\n", winsvc.ServiceName)
	self, _ := os.Executable()
	deferred, err := winsvc.RemoveInstalled(self)
	if err != nil {
		return err
	}
	if deferred {
		fmt.Printf("%s will be deleted at the next reboot (it is the program running now).\n", winsvc.InstallDir())
	} else {
		fmt.Printf("Removed %s.\n", winsvc.InstallDir())
	}
	p := config.DefaultPaths()
	dir := filepath.Dir(p.Config)
	if *purge {
		if err := os.RemoveAll(dir); err != nil {
			return err
		}
		fmt.Printf("Deleted %s (config, token, logs). The bridge must be retired on the hub too.\n", dir)
	} else {
		fmt.Printf("Kept %s (config, token, logs); uninstall --purge deletes it.\n", dir)
	}
	return nil
}
