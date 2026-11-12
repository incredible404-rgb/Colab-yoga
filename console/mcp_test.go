package main

import (
	"errors"
	"strings"
	"testing"

	"github.com/egoist/mygo/ui"
)

func TestReadMCPStartReadsTheBridgeAnswer(t *testing.T) {
	started := readMCPStart(map[string]any{"ok": true, "state": "started", "error": ""}, nil)
	if !started.OK || started.State != "started" || started.Error != "" {
		t.Errorf("started: got %+v", started)
	}
	failed := readMCPStart(map[string]any{"ok": false, "state": "failed", "error": "端口 3426 被其他程序占用"}, nil)
	if failed.OK || failed.Error != "端口 3426 被其他程序占用" {
		t.Errorf("failed: got %+v", failed)
	}
	silent := readMCPStart(map[string]any{"ok": false}, nil)
	if silent.OK || silent.Error == "" {
		t.Errorf("a failure without a reason must still carry one: got %+v", silent)
	}
	none := readMCPStart(nil, errors.New("bridge gave no JSON"))
	if none.OK || !strings.Contains(none.Error, "bridge gave no JSON") {
		t.Errorf("a call with no answer must fail and say why: got %+v", none)
	}
}

// With the GPU off and the local MCP down, the MCP card still offers the start; a click
// starts it in the background and does not wake a GPU.
func TestMCPCardStartsWithTheGPUOff(t *testing.T) {
	a := fakeApp(t)
	stateIdle(a)
	a.access.MCPUp = false
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "没在运行") {
		t.Fatalf("the MCP card should say it is down; texts %q", tt.Texts())
	}
	if err := tt.Click("启动"); err != nil {
		t.Fatalf("the start button is missing with the GPU off: %v", err)
	}
	if !a.mcpStarting {
		t.Fatal("the click did not start the local MCP")
	}
	if a.waking || a.acct.Active != 0 {
		t.Error("starting the local MCP must not wake a GPU")
	}
	tt.Frame()
	if !hasTextPart(tt, "正在启动本机 MCP 桥") {
		t.Errorf("the card does not show the start in progress; texts %q", tt.Texts())
	}
}

// While a start runs the card's button is busy, and a second request does nothing.
func TestMCPStartCannotBeDoubled(t *testing.T) {
	a := fakeApp(t)
	stateIdle(a)
	a.access.MCPUp = false
	a.mcpStartErr = "上一次的原因"
	a.startMCP()
	if !a.mcpStarting || a.mcpStartErr != "" {
		t.Fatalf("first start: starting=%v err=%q", a.mcpStarting, a.mcpStartErr)
	}
	a.startMCP()
	if !a.mcpStarting {
		t.Error("a second start while one runs must not end the first")
	}
	if m := a.mcpManual(); !m.busy || m.label != "启动中" {
		t.Errorf("the card's button while starting: %+v", m)
	}
}

// A failed start stays on the card with its real reason and turns the button into a retry.
func TestMCPStartFailureShowsItsReasonAndOffersRetry(t *testing.T) {
	a := fakeApp(t)
	stateIdle(a)
	a.access.MCPUp = false
	a.mcpStarting = true
	a.finishMCPStart(mcpStart{State: "failed", Error: "端口 3426 被其他程序占用"})
	if a.mcpStarting {
		t.Error("a finished start still reads as in flight")
	}
	if last := a.logs[len(a.logs)-1]; !strings.Contains(last, "端口 3426 被其他程序占用") {
		t.Errorf("the log lacks the reason: %q", last)
	}
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "手动启动没成功：端口 3426 被其他程序占用") {
		t.Errorf("the card lacks the failure reason; texts %q", tt.Texts())
	}
	if _, ok := tt.Find("重试"); !ok {
		t.Errorf("the card offers no retry after a failure; texts %q", tt.Texts())
	}
}

// A successful start clears the old failure and, once the health is read again, the
// card shows the server up with no start button left.
func TestMCPStartBusyJobDoesNotLeaveTheButtonStuck(t *testing.T) {
	a := fakeApp(t)
	a.inflight.Store("mcp-start", true)
	a.startMCP()
	if a.mcpStarting {
		t.Fatal("a rejected duplicate job must not leave the button in the starting state")
	}
}

func TestMCPStartSuccessClearsTheFailure(t *testing.T) {
	a := fakeApp(t)
	stateIdle(a)
	a.mcpStarting, a.mcpStartErr = true, "旧的失败原因"
	a.finishMCPStart(mcpStart{OK: true, State: "started"})
	a.access.MCPUp = true // what the refresh after a start reads from the port
	if a.mcpStartErr != "" || a.mcpStarting {
		t.Fatalf("success left starting=%v err=%q", a.mcpStarting, a.mcpStartErr)
	}
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if hasTextPart(tt, "旧的失败原因") {
		t.Error("the old failure is still on the card")
	}
	if _, ok := tt.Find("重试"); ok {
		t.Error("the card still offers a retry after a success")
	}
}
