//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

const (
	createNoWindow         = 0x08000000
	detachedProcess        = 0x00000008
	createNewProcessGroup  = 0x00000200
	createBreakawayFromJob = 0x01000000
	stillActive            = 259
)

func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
}

// startDetached starts cmd outside this process's job and console, so it survives the
// console closing (the keeper must). Breakaway is refused inside some jobs; then plain.
func startDetached(cmd *exec.Cmd) error {
	base := uint32(detachedProcess | createNewProcessGroup | createNoWindow)
	for _, flags := range []uint32{base | createBreakawayFromJob, base} {
		c := exec.Command(cmd.Path, cmd.Args[1:]...)
		c.Env, c.Dir = cmd.Env, cmd.Dir
		c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: flags}
		if err := c.Start(); err == nil {
			_ = c.Process.Release()
			return nil
		} else if flags == base {
			return err
		}
	}
	return nil
}

func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	h, err := syscall.OpenProcess(0x1000, false, uint32(pid)) // PROCESS_QUERY_LIMITED_INFORMATION
	if err != nil {
		return false
	}
	defer syscall.CloseHandle(h)
	var code uint32
	if syscall.GetExitCodeProcess(h, &code) != nil {
		return false
	}
	return code == stillActive
}

var procSetProcessWorkingSetSize = syscall.NewLazyDLL("kernel32.dll").NewProc("SetProcessWorkingSetSize")

// trimWorkingSet asks Windows to page out what this process is not using (-1, -1),
// as Task Manager's numbers drop for an app minimized to the tray.
func trimWorkingSet() {
	h, err := syscall.GetCurrentProcess()
	if err != nil {
		return
	}
	all := ^uintptr(0)
	_, _, _ = procSetProcessWorkingSetSize.Call(uintptr(h), all, all)
}
