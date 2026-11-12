//go:build windows

package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"

	"github.com/egoist/mygo"
)

func TestMain(m *testing.M) {
	if os.Getenv("COLAB_YOGA_TRAY_LIFECYCLE_TEST") == "1" {
		os.Exit(checkTrayLifecycle())
	}
	os.Exit(m.Run())
}

// The real native loop must run on the process's main thread, in its own process.
// This exercises makeTray and the same Quit used by the tray menu, without the
// account bridge, settings or GPU loop.
func checkTrayLifecycle() int {
	a := &App{}
	var tray *mygo.Tray
	var failure string
	mygo.App.WhenReady(func() {
		a.makeTray()
		tray = a.tray
		if tray == nil {
			failure = "makeTray did not create a tray"
			mygo.App.Quit()
			return
		}
		go func() {
			time.Sleep(300 * time.Millisecond)
			mygo.RunOnMain(func() {
				if bounds := tray.Bounds(); bounds.Width == 0 || bounds.Height == 0 {
					failure = "tray was not registered with the Windows shell"
				}
				mygo.App.Quit()
			})
		}()
	})
	if err := mygo.App.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if tray != nil {
		if bounds := tray.Bounds(); bounds.Width != 0 || bounds.Height != 0 {
			failure = fmt.Sprintf("tray remains registered after Quit: %+v", bounds)
		}
	}
	if failure != "" {
		fmt.Fprintln(os.Stderr, failure)
		return 1
	}
	return 0
}

func TestTrayRemovedOnQuit(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, exe)
	cmd.Env = append(os.Environ(), "COLAB_YOGA_TRAY_LIFECYCLE_TEST=1")
	hideWindow(cmd)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("native tray lifecycle: %v\n%s", err, output)
	}
}
