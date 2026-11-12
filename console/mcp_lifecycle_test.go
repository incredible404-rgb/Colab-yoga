package main

import (
	"errors"
	"testing"
)

func TestMCPCardWaitsForConfirmedCloudReachability(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*App)
	}{
		{"disconnected", func(a *App) { a.connected, a.status = false, nil }},
		{"not checked yet", func(a *App) { a.status.LLM.MCP.Reachable = nil }},
		{"missing cloud status", func(a *App) { a.status.LLM.MCP = nil }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := fakeApp(t)
			if w := wayInNamed(t, a, "MCP 桥"); w.state != "ok" {
				t.Fatalf("confirmed baseline must be green: %+v", w)
			}
			tc.change(a)
			if w := wayInNamed(t, a, "MCP 桥"); w.state != "idle" {
				t.Errorf("local health alone must not claim a confirmed cloud path: %+v", w)
			}
		})
	}
}

func TestReleaseInvalidatesCachedModelAndMCPHealth(t *testing.T) {
	a := fakeApp(t)
	a.finishRelease(map[string]any{"active_assignments": float64(0), "burn_rate_hourly": float64(0)}, nil)
	if _, alias, online := a.servedModel(); online {
		t.Errorf("released model resurrected from the cached tailnet probe: %q", alias)
	}
	if a.access.MCPUp || !a.access.At.IsZero() {
		t.Error("release must invalidate health checked before the local bridge could stop")
	}
	if a.acct.Active != 0 || a.connected || a.status != nil {
		t.Errorf("confirmed release left runtime state: account=%+v connected=%v status=%+v", a.acct, a.connected, a.status)
	}
	if w := wayInNamed(t, a, "MCP 桥"); w.state == "ok" {
		t.Error("release must not claim that the cloud MCP is connected")
	}
}

func TestUnconfirmedReleaseDoesNotDiscardTheRuntime(t *testing.T) {
	for _, tc := range []struct {
		name   string
		result map[string]any
		err    error
	}{
		{"command failure", nil, errors.New("release failed")},
		{"API failure", map[string]any{"error": "allocation lookup failed"}, nil},
		{"missing confirmation", map[string]any{}, nil},
		{"remaining allocation", map[string]any{"active_assignments": float64(1), "burn_rate_hourly": float64(10)}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := fakeApp(t)
			previousURL, previousStatus := a.serverURL, a.status
			a.finishRelease(tc.result, tc.err)
			if !a.connected || a.serverURL != previousURL || a.status != previousStatus || a.acct.Active == 0 {
				t.Errorf("an unconfirmed release must preserve the last known runtime: connected=%v active=%d", a.connected, a.acct.Active)
			}
		})
	}
}

func TestLateAccessProbeCannotResurrectAReleasedSession(t *testing.T) {
	a := fakeApp(t)
	oldEpoch, oldAccess := a.accessEpoch, a.access
	a.finishRelease(map[string]any{"active_assignments": float64(0), "burn_rate_hourly": float64(0)}, nil)
	a.applyAccess(oldEpoch, oldAccess)
	if _, _, online := a.servedModel(); online || a.access.MCPUp {
		t.Error("a probe started before release restored stale model/MCP health")
	}
	fresh := Access{At: a.now(), MCPUp: true}
	a.applyAccess(a.accessEpoch, fresh)
	if !a.access.MCPUp || a.access.Llama.OK {
		t.Error("a new probe must still update local health after release")
	}
	if w := wayInNamed(t, a, "MCP 桥"); w.state != "idle" {
		t.Errorf("a locally healthy MCP with no cloud must be identified separately: %+v", w)
	}
}
