package main

import (
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/egoist/mygo/ui"
)

// fakeApp is a connected console showing realistic data, with no window, network,
// bridge or settings file behind it.
func fakeApp(t *testing.T) *App {
	t.Helper()
	dir := t.TempDir()
	p := Paths{ConfigDir: dir, Settings: filepath.Join(dir, "settings.json"), Keeper: filepath.Join(dir, "keeper.json")}
	a := newApp(p)
	a.disableBackground = true
	a.tsURL = "http://qwen.example.ts.net:443"
	a.novelRoot = `C:\Users\example\Documents\Colab创作`
	a.serverURL = "https://abcdef.gradio.invalid"
	a.acct = Account{Known: true, LoggedIn: true, Email: "someone@gmail.com", Balance: 2795.70, Burn: 10.05,
		Active: 1, Endpoint: "gpu-g4-s-asso1c0-3i88lxeiohgl5", Accel: "G4"}
	yes := true
	a.applyStatus(&Status{
		GPUName: "NVIDIA RTX PRO 6000 Blackwell Server Edition", VRAMUsed: 52.4, VRAMTotal: 95.6,
		RAMUsed: 67.9, RAMTotal: 177, DiskUsed: 48.1, DiskTotal: 236, Uptime: 4790, SessionCU: 13.4, Burn: 10.05,
		IdleTimeoutMin: 30, IdleRemaining: 1560,
		LLM: &LLMServer{Online: true, Alias: "Gemma-4-31B-it-ARA-Heretic-Q8_0", ModelID: "gemma4-31b",
			SlotsTotal: 1, NCtx: 262144, LastActiveAge: 190, MCP: &MCPBridge{Listening: true, Owner: "proc", Reachable: &yes}},
		Console: []string{"[01:13:04] Running on public URL", "[01:13:05] [MCP 桥] 已在 127.0.0.1:3426 常驻"},
	})
	for i := 0; i < 40; i++ {
		a.applyStatus(a.status)
	}
	up := true
	a.access = Access{At: time.Now(), Llama: LlamaProbe{OK: true, Alias: "Gemma-4-31B-it-ARA-Heretic-Q8_0", NCtx: 262144, SlotsTotal: 1},
		Magpie: MagpieProbe{ProviderID: "qwen-colab", ProviderName: "Colab Qwen", Off: true, GatewayUp: &up, Listed: true},
		MCPUp: true}
	a.keeper = Keeper{Alive: true, Beats: 120}
	return a
}

func TestDashboardShowsWhatMatters(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	for _, want := range []string{"算力账单", "当前模型", "2,795.70 CU", "10.05 CU/h", "01:19:50", "13.4 CU",
		"Gemma-4-31B (ARA Heretic Q8_0)", "已连通", "新加坡 asso1", "后台保活运行中：关掉这个窗口也不影响 GPU"} {
		if !hasTextPart(tt, want) {
			t.Errorf("dashboard lacks %q; texts %q", want, tt.Texts())
		}
	}
}

func TestControlsFitTheWindow(t *testing.T) {
	for _, size := range [][2]int{{1440, 900}, {1000, 660}, {1920, 1080}} {
		a := fakeApp(t)
		tt := ui.NewTesterWith(a.view, size[0], size[1], ui.TesterOptions{Dark: true})
		for _, label := range []string{"打开网页对话", "续时", "释放", "日志", "切换模型 ▾", "卸载", "仪表盘", "连接与账号"} {
			r, ok := tt.Find(label)
			if ok && r.W == 0 && r.Y >= float32(size[1]) {
				continue // below the fold: the page scrolls vertically, never sideways
			}
			if !ok || r.W <= 0 || r.X < 0 || r.X+r.W > float32(size[0])+0.5 {
				t.Errorf("%dx%d: %q at %+v (found %v)", size[0], size[1], label, r, ok)
			}
		}
	}
}

func TestSwitchAsksFirst(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if err := tt.Click("切换到 Qwen3.8-Flash-Next (Huihui v2)"); err != nil {
		t.Fatal(err)
	}
	if !a.askSwitch || a.switchTarget != "qwen38-huihui-v2" || a.switching != "" {
		t.Fatalf("switch must ask before loading: ask=%v target=%q switching=%q", a.askSwitch, a.switchTarget, a.switching)
	}
	if !hasTextPart(tt, "会先停掉它") {
		t.Errorf("confirmation should warn that :8081 is replaced; texts %q", tt.Texts())
	}
}

func TestReleaseAsksFirst(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if err := tt.Click("释放"); err != nil {
		t.Fatal(err)
	}
	if !a.askRelease || a.releasing {
		t.Fatalf("release must ask first: ask=%v releasing=%v", a.askRelease, a.releasing)
	}
}

func TestTrayOnlyPollsSlowlyAndNeverProbes(t *testing.T) {
	var status, access int
	for tick := 1; tick <= 120; tick++ { // 5 minutes
		d := schedule(tick, true, true, 1)
		if d.status {
			status++
		}
		if d.access {
			access++
		}
	}
	if status != 10 || access != 0 {
		t.Errorf("tray only: %d status polls, %d probes in 5 min; want 10, 0", status, access)
	}
	if d := schedule(4, false, true, 1); !d.status || !d.access {
		t.Errorf("shown and connected must poll status and probe: %+v", d)
	}
}

func TestPathsAndLegacyFallback(t *testing.T) {
	local := t.TempDir()
	p := pathsFor(local, filepath.Join(local, "Programs", "Colab-yoga"))
	if !strings.HasSuffix(p.Bridge, filepath.Join("Colab-yoga", "bridge", "colab_yoga_bridge.py")) ||
		!strings.HasSuffix(p.Python, filepath.Join("Colab-yoga", "runtime", "python.exe")) ||
		p.Settings != filepath.Join(local, "Colab-yoga", "settings.json") {
		t.Errorf("paths %+v", p)
	}
	if p.runnersFile() != "" {
		t.Error("no runner file exists yet")
	}
	legacy := p.Runners[1]
	if err := os.MkdirAll(filepath.Dir(legacy), 0o755); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(legacy, []byte("{}"), 0o644)
	if p.runnersFile() != legacy {
		t.Errorf("legacy runners not found: %q", p.runnersFile())
	}
	_ = os.MkdirAll(filepath.Dir(p.Runners[0]), 0o755)
	_ = os.WriteFile(p.Runners[0], []byte("{}"), 0o644)
	future := time.Now().Add(time.Hour)
	_ = os.Chtimes(p.Runners[0], future, future)
	if p.runnersFile() != p.Runners[0] {
		t.Errorf("the newer runner file must win: %q", p.runnersFile())
	}
}

func TestNoStudioOrMultimodalLeft(t *testing.T) {
	a := fakeApp(t)
	for _, page := range []string{"dashboard", "connection"} {
		a.page = page
		tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
		for _, gone := range []string{"创作工作室", "旧版", "ComfyUI", "MiniMax", "EasyColab"} {
			if hasTextPart(tt, gone) {
				t.Errorf("%s page still mentions %q", page, gone)
			}
		}
		if !hasTextPart(tt, "Colab-yoga") {
			t.Errorf("%s page lacks the Colab-yoga brand", page)
		}
	}
}

func TestBindingShowsCodeInputAndCancels(t *testing.T) {
	a := fakeApp(t)
	a.acct.LoggedIn = false
	a.page = "connection"
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "绑定 Google 账号") {
		t.Fatalf("no bind button; texts %q", tt.Texts())
	}
	// The bridge step (auth_url) runs in the background; with it done, the code input shows.
	a.binding = true
	tt.Frame()
	if _, ok := tt.Find("授权码"); !ok {
		t.Fatalf("no authorization code input; texts %q", tt.Texts())
	}
	if err := tt.Click("取消"); err != nil {
		t.Fatal(err)
	}
	if a.binding || a.authCode != "" {
		t.Error("cancel must leave binding mode and drop the code")
	}
}

func TestLogoutAsksFirst(t *testing.T) {
	a := fakeApp(t)
	a.page = "connection"
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if err := tt.Click("退出账号"); err != nil {
		t.Fatal(err)
	}
	if !a.askLogout {
		t.Fatal("logout must ask first")
	}
}

func TestUnloadAsksFirst(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if err := tt.Click("卸载"); err != nil {
		t.Fatal(err)
	}
	if !a.askUnload {
		t.Fatal("unload must ask first")
	}
	if !hasTextPart(tt, "GPU 仍在计费") {
		t.Errorf("unload confirmation should say the GPU still bills; texts %q", tt.Texts())
	}
}

func TestAccountErrorIsNotAnEmptyLedger(t *testing.T) {
	if _, err := parseAccount(map[string]any{"ok": false, "logged_in": true, "error": "429"}); err != "429" {
		t.Errorf("ok:false must be an error, got %q", err)
	}
	acct, err := parseAccount(map[string]any{"ok": true, "logged_in": true, "paid_balance": 2794.0,
		"burn_rate_hourly": 10.05, "active_assignments": 1.0,
		"sessions": []any{map[string]any{"endpoint": "gpu-g4-s-asso1c0-x", "accelerator": "G4"}}})
	if err != "" || !acct.Known || acct.Balance != 2794 || acct.Active != 1 || acct.Accel != "G4" {
		t.Errorf("parsed %+v err %q", acct, err)
	}
}

func TestMagpieListedIsTheProvidersOwnList(t *testing.T) {
	m := MagpieProbe{ProviderID: "qwen-colab", Models: []string{"Gemma-4-31B-it-ARA-Heretic-Q8_0"}}
	if !m.lists("Gemma-4-31B-it-ARA-Heretic-Q8_0") || m.lists("Gemma-4-31B") || m.lists("") {
		t.Error("membership must be exact and per provider")
	}
}

func TestUnreadableSettingsAreNotOverwritten(t *testing.T) {
	dir := t.TempDir()
	s := &Settings{path: filepath.Join(dir, "settings.json")}
	half := []byte(`{"apiAccessKey":"k","darkM`)
	if err := os.WriteFile(s.path, half, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := s.Set(map[string]any{"consoleTheme": "light"}); err == nil {
		t.Error("writing over a half-read settings.json must fail")
	}
	if b, _ := os.ReadFile(s.path); string(b) != string(half) {
		t.Errorf("settings.json was rewritten: %s", b)
	}
}

func TestOfflineAndRegion(t *testing.T) {
	a := fakeApp(t)
	a.connected, a.status, a.acct.Active, a.acct.Burn = false, nil, 0, 0
	a.access = Access{At: time.Now(), Llama: LlamaProbe{Err: "超时"}}
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	for _, want := range []string{"没有运行中的实例，不在计费", "未计费", "云端还没有模型在跑"} {
		if !hasTextPart(tt, want) {
			t.Errorf("offline view lacks %q; texts %q", want, tt.Texts())
		}
	}
	if code, name := regionOf("gpu-g4-s-euw4a2-27wx9foccsg7v"); code != "euw4" || name != "荷兰" {
		t.Errorf("region %q %q", code, name)
	}
}

func TestSettingsMergeKeepsOtherKeys(t *testing.T) {
	dir := t.TempDir()
	s := &Settings{path: filepath.Join(dir, "settings.json")}
	if err := os.WriteFile(s.path, []byte(`{"apiAccessKey":"k","darkMode":true}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := s.Set(map[string]any{"consoleTheme": "light"}); err != nil {
		t.Fatal(err)
	}
	m := s.Load()
	if m["apiAccessKey"] != "k" || m["darkMode"] != true || m["consoleTheme"] != "light" {
		t.Errorf("merged settings %v", m)
	}
}

// TestSnapshots writes PNGs for review: SNAP_DIR=... go test -run Snapshots
func TestSnapshots(t *testing.T) {
	dir := os.Getenv("SNAP_DIR")
	if dir == "" {
		t.Skip("set SNAP_DIR to write screenshots")
	}
	shots := []struct {
		name  string
		w, h  int
		setup func(a *App)
	}{
		{"dashboard_dark_1440", 1440, 900, nil},
		{"dashboard_light_1440", 1440, 900, func(a *App) { a.themeName = "light" }},
		{"dashboard_dark_1000", 1000, 660, nil},
		{"dashboard_dark_1920", 1920, 1080, nil},
		{"connection_dark_1440", 1440, 900, func(a *App) { a.page = "connection" }},
		{"logs_dark_1440", 1440, 900, func(a *App) { a.logsOpen = true }},
		{"switch_dialog_dark", 1440, 900, func(a *App) { a.switchTarget, a.askSwitch = "qwen38-huihui-v2", true }},
		{"loading_dark_1440", 1440, 900, func(a *App) {
			a.switching = "qwen38-huihui-v2"
			a.status.IsLoading, a.status.TaskProgress, a.status.TaskMessage = true, 0.46, "并发下载进度: 46% (49.1 / 107.1 GiB)，均速 3135 MB/s"
		}},
		{"offline_light_1440", 1440, 900, func(a *App) {
			a.themeName = "light"
			a.connected, a.status, a.acct.Active, a.acct.Burn = false, nil, 0, 0
			a.access = Access{At: time.Now(), Llama: LlamaProbe{Err: "超时"}, MCPUp: true}
			a.keeper = Keeper{}
		}},
	}
	for _, s := range shots {
		a := fakeApp(t)
		if s.setup != nil {
			s.setup(a)
		}
		tt := ui.NewTesterWith(a.view, s.w, s.h, ui.TesterOptions{Dark: a.themeName != "light"})
		tt.Frame()
		f, err := os.Create(filepath.Join(dir, s.name+".png"))
		if err != nil {
			t.Fatal(err)
		}
		if err := png.Encode(f, tt.Image()); err != nil {
			t.Fatal(err)
		}
		f.Close()
	}
}

func hasTextPart(tt *ui.Tester, part string) bool {
	for _, s := range tt.Texts() {
		if strings.Contains(s, part) {
			return true
		}
	}
	return false
}
