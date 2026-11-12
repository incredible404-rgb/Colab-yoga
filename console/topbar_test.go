package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/egoist/mygo/ui"
)

// The states a session passes through, each made from the connected fakeApp.
func stateIdle(a *App) {
	a.connected, a.status, a.acct.Active, a.acct.Burn, a.serverURL = false, nil, 0, 0, ""
	a.access = Access{At: time.Now(), Llama: LlamaProbe{Err: "超时"}}
	a.keeper = Keeper{}
	a.hist = [3][]float64{}
}

func stateWaking(a *App) {
	stateIdle(a)
	a.waking, a.wakeAt = true, a.now().Add(-78*time.Second)
	a.noteWake(cleanWakeLine("WAKE_LOG: 正在上传 Colab-yoga 服务端代码 (colab_yoga_server.py)..."))
}

func stateWakeFailed(a *App) {
	stateIdle(a)
	a.wakeStage, a.wakeErr = 0, "Colab 暂时没有空闲的 G4，稍后再试或换一个型号"
}

func stateLost(a *App) {
	stateIdle(a)
	a.acct.Active, a.acct.Burn = 1, 10.05
}

func stateNoModel(a *App) {
	a.status.LLM.Online = false
	a.access.Llama = LlamaProbe{Err: "超时"}
}

func stateLoading(a *App) {
	stateNoModel(a)
	a.switching, a.switchAt = "qwen38-huihui-v2", a.now().Add(-96*time.Second)
	a.status.IsDownloading, a.status.TaskProgress = true, 0.46
	a.status.TaskMessage = "并发下载进度: 46% (49.1 / 107.1 GiB)，均速 3135 MB/s"
}

func stateKeepWarning(a *App) {
	a.status.IdleRemaining = 252
	a.applyStatus(a.status)
}

// inkButtons counts the filled (ink) buttons in the top bar by looking at the frame: runs
// of the ink color at least a button's padding wide, on a line through the buttons above
// their labels. The two halves of the split wake button, a hairline apart, count as one.
func inkButtons(t *testing.T, tt *ui.Tester, p Palette) int {
	t.Helper()
	r, ok := tt.Find("日志") // the label's box: the buttons' padding is the 7 DIPs above it
	if !ok || r.Y < 8 || r.Y > 60 {
		t.Fatalf("no top bar to measure: %+v", r)
	}
	img := tt.Image() // one pixel per DIP at the tester's default scale
	want := p.Ink
	near := func(a, b uint8) bool { d := int(a) - int(b); return d > -4 && d < 4 }
	best := 0
	for _, dy := range []float32{3, 4, 5} {
		y := int(r.Y - dy)
		count, run, gap := 0, 0, 99
		for x := img.Bounds().Min.X; x < img.Bounds().Max.X; x++ {
			c := img.RGBAAt(x, y)
			if near(c.R, want.R) && near(c.G, want.G) && near(c.B, want.B) {
				run++
				continue
			}
			if run >= 20 {
				if gap > 3 {
					count++
				}
				gap = 0
			}
			run = 0
			gap++
		}
		if run >= 20 && gap > 3 {
			count++
		}
		best = max(best, count)
	}
	return best
}

// TestTopBarFollowsTheSession: at every state the top bar offers the step that comes next,
// filled, and nothing else is filled; what cannot be used is there but does nothing.
func TestTopBarFollowsTheSession(t *testing.T) {
	states := []struct {
		name         string
		setup        func(a *App)
		shown, gone  []string
		ink          int
		next         string            // the step to click
		did          func(a *App) bool // what clicking it must have done
		inert        []string          // shown, but a click changes nothing
		releaseReady bool
	}{
		{"no instance", stateIdle, []string{"唤醒 G4", "选择 GPU 型号", "加载模型", "打开网页对话", "续时", "释放", "没有实例 · 不计费"},
			[]string{"重新部署", "已唤醒", "已加载"}, 1, "唤醒 G4", func(a *App) bool { return a.waking },
			[]string{"加载模型", "打开网页对话", "续时", "释放"}, false},
		{"waking", stateWaking, []string{"唤醒中…", "正在部署服务端…", "01:18", "加载模型", "打开网页对话", "释放"},
			[]string{"唤醒 G4", "选择 GPU 型号", "已唤醒", "已加载"}, 0, "", nil, []string{"加载模型", "打开网页对话", "续时"}, true},
		{"billing without its server", stateLost, []string{"重新部署", "加载模型", "打开网页对话", "释放"},
			[]string{"唤醒 G4", "已唤醒", "已加载"}, 0, "重新部署", func(a *App) bool { return a.waking },
			[]string{"加载模型", "打开网页对话", "续时"}, true},
		{"connected, no model", stateNoModel, []string{"已唤醒", "加载模型", "打开网页对话", "续时", "释放"},
			[]string{"唤醒 G4", "选择 GPU 型号", "重新部署", "已加载"}, 1, "", nil, []string{"打开网页对话"}, true},
		{"loading a model", stateLoading, []string{"已唤醒", "装载中 46%", "打开网页对话", "续时", "释放"},
			[]string{"唤醒 G4", "加载模型", "已加载"}, 0, "", nil, []string{"打开网页对话"}, true},
		{"model online", nil, []string{"已唤醒", "已加载", "打开网页对话", "续时", "释放"},
			[]string{"唤醒 G4", "选择 GPU 型号", "加载模型", "重新部署"}, 1, "打开网页对话",
			func(a *App) bool { return len(a.opened) == 1 && a.opened[0] == a.tsURL }, nil, true},
		{"online, no address to open", func(a *App) { a.tsURL = "" }, []string{"已唤醒", "已加载", "打开网页对话", "续时", "释放"},
			[]string{"唤醒 G4", "加载模型"}, 0, "", nil, []string{"打开网页对话"}, true},
		{"account not read yet", func(a *App) { stateIdle(a); a.acct = Account{} }, []string{"唤醒 G4", "正在查询 Colab 账号…", "加载模型", "释放"},
			[]string{"重新部署", "已唤醒", "已加载"}, 0, "", nil, []string{"唤醒 G4", "加载模型", "打开网页对话", "续时", "释放"}, false},
		{"releasing", func(a *App) { a.releasing = true }, []string{"正在释放实例…", "已唤醒", "已加载", "打开网页对话", "释放"},
			[]string{"唤醒 G4", "加载模型"}, 1, "", nil, []string{"释放"}, false},
	}
	for _, s := range states {
		for _, width := range []int{1440, 1024} {
			a := fakeApp(t)
			if s.setup != nil {
				s.setup(a)
			}
			tt := ui.NewTesterWith(a.view, width, 640, ui.TesterOptions{Dark: true})
			tt.Advance(2 * time.Second)
			for _, want := range s.shown {
				if !hasTextPart(tt, want) {
					t.Errorf("%s @%d: lacks %q; texts %q", s.name, width, want, tt.Texts())
				}
			}
			for _, gone := range s.gone {
				if _, ok := tt.Find(gone); ok {
					t.Errorf("%s @%d: %q should not show", s.name, width, gone)
				}
			}
			// Money is the bill card's: the bar shows neither the balance nor the rate.
			for _, text := range tt.Texts() {
				if strings.HasPrefix(text, "余额 ") {
					t.Errorf("%s @%d: the top bar should not show %q", s.name, width, text)
				}
			}
			// The three steps keep their places in order, whatever state each is in.
			at := func(labels ...string) float32 {
				for _, l := range labels {
					if r, ok := tt.Find(l); ok && r.Y < 60 {
						return r.X
					}
				}
				t.Errorf("%s @%d: none of %q is in the top bar", s.name, width, labels)
				return -1
			}
			if wake, load, chat := at("唤醒 G4", "唤醒中…", "重新部署", "已唤醒"), at("加载模型", "装载中 46%", "已加载"), at("打开网页对话"); !(wake < load && load < chat) {
				t.Errorf("%s @%d: the steps must stay in order: wake %.0f, load %.0f, chat %.0f", s.name, width, wake, load, chat)
			}
			for _, label := range []string{"续时", "释放", "日志"} {
				if r, ok := tt.Find(label); !ok || r.W <= 0 || r.X+r.W > float32(width)+0.5 || r.Y > 60 {
					t.Errorf("%s @%d: %q not in the top bar: %+v", s.name, width, label, r)
				}
			}
			if n := inkButtons(t, tt, darkPalette); n != s.ink {
				t.Errorf("%s @%d: %d filled buttons in the top bar, want %d", s.name, width, n, s.ink)
			}
			for _, label := range s.inert {
				if err := tt.Click(label); err != nil {
					t.Errorf("%s @%d: %q should still be there to hover: %v", s.name, width, label, err)
				}
			}
			if a.askRelease || len(a.opened) > 0 || (a.waking && s.name != "waking") {
				t.Errorf("%s @%d: a grayed-out button did something: release=%v opened=%q waking=%v", s.name, width, a.askRelease, a.opened, a.waking)
			}
			if s.next != "" {
				if err := tt.Click(s.next); err != nil {
					t.Fatalf("%s @%d: %v", s.name, width, err)
				}
				if !s.did(a) {
					t.Errorf("%s @%d: clicking %q did not do its step", s.name, width, s.next)
				}
			}
			if s.releaseReady {
				tt.Frame()
				if err := tt.Click("释放"); err != nil || !a.askRelease {
					t.Errorf("%s @%d: release must be available and ask first: err=%v ask=%v", s.name, width, err, a.askRelease)
				}
			}
		}
	}
}

func TestWakeButtonPicksTheGPU(t *testing.T) {
	a := fakeApp(t)
	stateIdle(a)
	tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
	if err := tt.Click("选择 GPU 型号"); err != nil {
		t.Fatal(err)
	}
	if got := tt.Menu(); !slices.Equal(got, gpuChoices) {
		t.Fatalf("GPU menu %q, want %q", got, gpuChoices)
	}
	if err := tt.ChooseMenuItem("T4"); err != nil {
		t.Fatal(err)
	}
	tt.Frame()
	if a.gpu != "T4" || str(a.set.Load(), "wakeGPUChoiceV2", "") != "T4" || !hasTextPart(tt, "唤醒 T4") {
		t.Errorf("gpu %q, saved %q, texts %q", a.gpu, str(a.set.Load(), "wakeGPUChoiceV2", ""), tt.Texts())
	}
	if a.waking {
		t.Error("choosing a GPU must not start a wake")
	}
}

// TestLoadButtonOffersTheLastModelFirst: the first click on the load button opens the
// library with the model loaded last time chosen and makes the button the confirmation;
// the second click loads it. Another model is one click more, and a click elsewhere
// backs out with nothing loaded.
func TestLoadButtonOffersTheLastModelFirst(t *testing.T) {
	open := func() (*App, *ui.Tester) {
		a := fakeApp(t)
		stateNoModel(a)
		a.lastModel = "gemma4-31b"
		tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
		tt.Advance(2 * time.Second)
		if err := tt.Click("加载模型"); err != nil {
			t.Fatal(err)
		}
		tt.Frame()
		tt.Advance(time.Second)
		if !a.pickOpen || a.pickID != "gemma4-31b" || a.switching != "" {
			t.Fatalf("the first click must only open the library on the last model: open=%v pick=%q switching=%q", a.pickOpen, a.pickID, a.switching)
		}
		return a, tt
	}

	a, tt := open()
	for _, want := range []string{"加载上次使用", "上次使用", a.modelName("qwen38-huihui-v2"), a.modelName("gemma4-31b")} {
		if _, ok := tt.Find(want); !ok {
			t.Errorf("the open library lacks %q; texts %q", want, tt.Texts())
		}
	}
	if _, ok := tt.Find("加载模型"); ok {
		t.Error("with the library open the button is the confirmation, not 加载模型")
	}
	for _, m := range a.catalog {
		if r, ok := tt.Find("选择 " + m.Name); !ok || r.X < 0 || r.X+r.W > 1024 || r.Y < 60 || r.Y+r.H > 640 {
			t.Errorf("%q must be offered inside the window, under the bar: %+v (found %v)", m.Name, r, ok)
		}
	}
	if err := tt.Click("加载上次使用"); err != nil {
		t.Fatal(err)
	}
	tt.Frame()
	if a.switching != "gemma4-31b" || a.pickOpen {
		t.Errorf("the second click must load the last model: switching=%q open=%v", a.switching, a.pickOpen)
	}
	if a.askSwitch {
		t.Error("a first load interrupts nothing and asks nothing")
	}

	a, tt = open()
	if err := tt.Click("选择 " + a.modelName("qwen38-huihui-v2")); err != nil {
		t.Fatal(err)
	}
	tt.Frame()
	if a.pickID != "qwen38-huihui-v2" || !a.pickOpen || a.switching != "" {
		t.Fatalf("choosing another model only chooses: pick=%q open=%v switching=%q", a.pickID, a.pickOpen, a.switching)
	}
	if err := tt.Click("加载所选"); err != nil {
		t.Fatalf("%v; texts %q", err, tt.Texts())
	}
	tt.Frame()
	if a.switching != "qwen38-huihui-v2" || a.pickOpen {
		t.Errorf("the button must load the model chosen: switching=%q open=%v", a.switching, a.pickOpen)
	}

	a, tt = open()
	tt.ClickAt(700, 520) // somewhere on the page
	tt.Frame()
	tt.Advance(time.Second)
	if _, ok := tt.Find("加载模型"); a.pickOpen || a.switching != "" || !ok {
		t.Errorf("a click elsewhere must back out: open=%v switching=%q, button back=%v", a.pickOpen, a.switching, ok)
	}

	// No model remembered, or one the library no longer lists: the first of the library.
	a = fakeApp(t)
	stateNoModel(a)
	a.lastModel = "gone-model"
	tt = ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
	if err := tt.Click("加载模型"); err != nil {
		t.Fatal(err)
	}
	tt.Frame()
	if _, ok := tt.Find("加载所选"); !ok || a.pickID != a.catalog[0].ID {
		t.Errorf("with no last model the first of the library is offered: pick=%q, button=%v", a.pickID, ok)
	}
}

// TestWakeStagesFollowTheBridge reads the stage from the bridge's own wording. The second
// half holds the two together: every stage must still be told by some line the bridge
// prints, so rewording a WAKE_LOG without teaching wakeWords fails here.
func TestWakeStagesFollowTheBridge(t *testing.T) {
	for _, l := range []struct {
		line  string
		stage int
		shown string
	}{
		{"WAKE_LOG: 🔍 正在检查账号下已有的 Colab 实例...", 0, "正在检查账号下已有的 Colab 实例…"},
		{"WAKE_LOG: 🚀 正在向 Google Colab 申请分配 G4 实例...", 0, "正在向 Google Colab 申请分配 G4 实例…"},
		{"WAKE_LOG: ✅ 成功分配 G4 云端虚拟机！正在等待容器内核就绪...", 1, "成功分配 G4 云端虚拟机！正在等待容器内核就绪…"},
		{"WAKE_LOG: ⏳ 容器仍在初始化，继续尝试连接内核...", 1, "容器仍在初始化，继续尝试连接内核…"},
		{"WAKE_LOG: ♻️ 正在检查已有实例 [yoga] (G4) 连通性...", 1, "正在检查已有实例 [yoga] (G4) 连通性…"},
		{"WAKE_LOG: 正在上传 Colab-yoga 服务端代码 (colab_yoga_server.py)...", 2, "正在上传 Colab-yoga 服务端代码 (colab_yoga_server.py)…"},
		{"WAKE_LOG: 正在下发控制接口令牌...", 2, "正在下发控制接口令牌…"},
		{"正在安装缺失依赖: fastapi uvicorn", 2, "正在安装缺失依赖: fastapi uvicorn"},
		{"WAKE_LOG: 已启动后台保活（独立进程，关掉这个命令也不会中断）", -1, "已启动后台保活（独立进程，关掉这个命令也不会中断）"},
	} {
		if got := cleanWakeLine(l.line); got != l.shown || wakeStageOf(got) != l.stage {
			t.Errorf("%q: shown %q stage %d, want %q stage %d", l.line, got, wakeStageOf(got), l.shown, l.stage)
		}
	}
	a := fakeApp(t)
	a.noteWake("正在下发控制接口令牌…")
	a.noteWake("已启动后台保活")
	a.acct.Active, a.disableBackground = 1, true
	a.wake()
	if !a.redeploying || a.wakeTitle() != "正在重新部署服务端" || a.wakeStage != 0 || a.wakeLine != "" {
		t.Errorf("a wake over a runtime that is assigned is a redeploy and starts its stages over: %v %q %d %q", a.redeploying, a.wakeTitle(), a.wakeStage, a.wakeLine)
	}
	a.waking = false
	a.noteWake("正在下发控制接口令牌…")
	a.noteWake("已启动后台保活")
	if a.wakeStage != 2 || a.wakeLine != "已启动后台保活" {
		t.Errorf("a line that marks no stage keeps the stage and is still the latest line: %d %q", a.wakeStage, a.wakeLine)
	}

	src, err := os.ReadFile(filepath.Join("..", "bridge", "colab_yoga_bridge.py"))
	if err != nil {
		t.Skipf("no bridge source beside the console: %v", err)
	}
	told := map[int]bool{}
	for _, m := range regexp.MustCompile(`WAKE_LOG: ([^"\n]*)`).FindAllStringSubmatch(string(src), -1) {
		told[wakeStageOf(m[1])] = true
	}
	for stage, name := range wakeStages[:len(wakeStages)-1] { // the last is told by WAKE_URL
		if !told[stage] {
			t.Errorf("no WAKE_LOG line of the bridge tells the stage %q any more", name)
		}
	}
}

func TestWakeShowsItsStagesAndWhereItFailed(t *testing.T) {
	a := fakeApp(t)
	stateWaking(a)
	tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
	tt.Advance(2 * time.Second)
	for _, want := range append([]string{"正在唤醒 G4", "唤醒中", "01:18", "正在上传 Colab-yoga 服务端代码 (colab_yoga_server.py)…"}, wakeStages...) {
		if !hasTextPart(tt, want) {
			t.Errorf("waking card lacks %q; texts %q", want, tt.Texts())
		}
	}
	a.waking, a.wakeErr = false, "Colab 暂时没有空闲的 G4"
	tt.Frame()
	tt.Advance(2 * time.Second)
	for _, want := range []string{"唤醒失败", "Colab 暂时没有空闲的 G4", "部署服务端", "唤醒 G4", "查看日志"} {
		if !hasTextPart(tt, want) {
			t.Errorf("failed wake lacks %q; texts %q", want, tt.Texts())
		}
	}
	if tt.FramePending() {
		t.Error("a failed wake is at rest: nothing pulses")
	}
	if err := tt.Click("知道了"); err != nil {
		t.Fatal(err)
	}
	tt.Advance(2 * time.Second)
	if a.wakeErr != "" || hasTextPart(tt, "唤醒失败") || !hasTextPart(tt, "还没有实例") {
		t.Errorf("dismissing must clear the failure: err %q texts %q", a.wakeErr, tt.Texts())
	}
}

func TestKeepButtonCountsDownTheLastFiveMinutes(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
	if hasTextPart(tt, "续时 ") {
		t.Errorf("26 minutes left is no warning; texts %q", tt.Texts())
	}
	stateKeepWarning(a)
	tt.Frame()
	if !hasTextPart(tt, "续时 4:12") {
		t.Errorf("4:12 left should show on the keep button; texts %q", tt.Texts())
	}
	if r, ok := tt.Find("续时"); !ok || r.Y > 60 {
		t.Errorf("the keep button must still be found by its name: %+v %v", r, ok)
	}
}

func TestClearingTheLogCanBeUndone(t *testing.T) {
	a := fakeApp(t)
	a.logs, a.logsOpen = []string{"12:00:00 one", "12:00:01 two"}, true
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	tt.Advance(2 * time.Second)
	if err := tt.Click("清空"); err != nil {
		t.Fatal(err)
	}
	if len(a.logs) != 0 || a.serverLog != nil {
		t.Fatalf("clear must clear at once: %q %q", a.logs, a.serverLog)
	}
	tt.Frame()
	tt.Advance(500 * time.Millisecond)
	if err := tt.Click("撤销"); err != nil {
		t.Fatalf("the toast should offer Undo: %v; texts %q", err, tt.Texts())
	}
	if len(a.logs) != 2 || len(a.serverLog) != 2 {
		t.Errorf("undo must bring the log back: %q %q", a.logs, a.serverLog)
	}
}

func TestNothingIsRedWithoutARuntime(t *testing.T) {
	a := fakeApp(t)
	stateIdle(a) // the local MCP service is down too
	for _, w := range a.waysIn() {
		if w.state == "down" {
			t.Errorf("no instance: %q shows as a fault (%s)", w.title, w.note)
		}
	}
	if text, col := a.mcpSummary(darkPalette); col == darkPalette.Danger {
		t.Errorf("no instance: MCP shows red %q", text)
	}
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if hasTextPart(tt, "硬件") || hasTextPart(tt, "推理槽位") {
		t.Errorf("no instance: empty charts and dashes still show; texts %q", tt.Texts())
	}
	stateLost(a)
	down := 0
	for _, w := range a.waysIn() {
		if w.state == "down" {
			down++
		}
	}
	if down == 0 {
		t.Error("a runtime that bills while its ways in fail must say so")
	}
}

func TestWaysInSayLessWhenTheyWork(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if hasTextPart(tt, "浏览器直接打开 llama-server 网页界面") {
		t.Error("a way in that works should keep its note for the tooltip")
	}
	if !hasTextPart(tt, "供应商在 Magpie 里是关闭的") {
		t.Errorf("a way in that needs attention must say what; texts %q", tt.Texts())
	}
	if !hasTextPart(tt, "已运行 01:19:50") || hasTextPart(tt, "这里管算力和模型") {
		t.Errorf("the subtitle should say the uptime and no more; texts %q", tt.Texts())
	}
}

func TestSignedOutWithAServerKeepsTheConsole(t *testing.T) {
	a := fakeApp(t)
	a.acct.LoggedIn = false
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	// A server still answers: releasing and keeping it alive must stay in reach.
	for _, want := range []string{"仪表盘", "释放", "续时", "登录 Google 账号"} {
		if _, ok := tt.Find(want); !ok {
			t.Errorf("signed out but connected: %q should show; texts %q", want, tt.Texts())
		}
	}
	if err := tt.Click("登录 Google 账号"); err != nil || !a.authBusy {
		t.Errorf("the rail's sign-in must start binding: err=%v busy=%v", err, a.authBusy)
	}
}

func TestThemeButtonSwitchesBothWays(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if err := tt.Click("切换到浅色"); err != nil || a.themeName != "light" {
		t.Fatalf("to light: err=%v theme=%q", err, a.themeName)
	}
	tt.Advance(time.Second)
	if err := tt.Click("切换到深色"); err != nil || a.themeName != "dark" {
		t.Fatalf("to dark: err=%v theme=%q", err, a.themeName)
	}
	if _, ok := tt.Find("配色"); ok {
		t.Error("the two-segment theme strip should be gone")
	}
}

// TestNarrowWindowKeepsWhatMatters: at the width the window is often left at, the status
// line is whole before a server is connected, and once one is it still opens with its state.
func TestNarrowWindowKeepsWhatMatters(t *testing.T) {
	natural := func(text string) float32 {
		tt := ui.NewTesterWith(func(c *ui.Context) {
			applyTheme(c, darkPalette)
			ui.Row(c).Children(func() { ui.Text(c, text).NoWrap() })
		}, 1200, 80, ui.TesterOptions{Dark: true})
		r, _ := tt.Find(text)
		return r.W
	}
	for name, setup := range map[string]func(a *App){"no instance": stateIdle, "waking": stateWaking, "wake failed": stateWakeFailed} {
		a := fakeApp(t)
		setup(a)
		_, text, _ := a.runtimeLine(darkPalette)
		tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
		tt.Advance(2 * time.Second)
		r, ok := tt.Find(text)
		if want := natural(text); !ok || want < 40 || r.W < want-0.5 {
			t.Errorf("%s: status line %q has %.0f of the %.0f DIPs it needs at 1024 wide (found %v)", name, text, r.W, want, ok)
		}
	}
	for name, setup := range map[string]func(a *App){"online": nil, "no model": stateNoModel, "keep warning": stateKeepWarning, "lost": stateLost} {
		a := fakeApp(t)
		if setup != nil {
			setup(a)
		}
		_, text, _ := a.runtimeLine(darkPalette)
		tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
		r, ok := tt.Find(text)
		state := "在线"
		if name == "lost" {
			state = "实例在计费"
		}
		if !ok || !strings.HasPrefix(text, state) || r.W < natural(state)+8 {
			t.Errorf("%s: the status line %q must open with %q and have room for it: %.0f DIPs (found %v)", name, text, state, r.W, ok)
		}
	}
}

// TestAccountStaysInReach: the account and the theme switch keep their place at the foot of
// the rail however long the model library is, and what they ask opens inside the window.
func TestAccountStaysInReach(t *testing.T) {
	a := fakeApp(t)
	for i := 0; i < 30; i++ {
		id := fmt.Sprintf("extra-%02d", i)
		a.catalog = append(a.catalog, Model{ID: id, Name: "Extra-Model-" + id, Alias: id})
	}
	tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
	tt.Advance(2 * time.Second)
	for _, label := range []string{"账号", "切换到浅色"} {
		if r, ok := tt.Find(label); !ok || r.W <= 0 || r.Y < 60 || r.Y+r.H > 640 {
			t.Errorf("%q must stay in view under a long library: %+v (found %v)", label, r, ok)
		}
	}
	a.askLogout = true
	tt.Frame()
	tt.Advance(time.Second)
	for _, label := range []string{"退出 Google 账号？", "确认退出", "取消"} {
		if r, ok := tt.Find(label); !ok || r.X < 0 || r.Y < 0 || r.X+r.W > 1024 || r.Y+r.H > 640 {
			t.Errorf("the sign-out question must open inside the window: %q at %+v (found %v)", label, r, ok)
		}
	}
	if err := tt.Click("取消"); err != nil || a.askLogout {
		t.Errorf("cancel must close the question: err=%v ask=%v", err, a.askLogout)
	}
}

// TestGrayedButtonsStayGrayUnderThePointer: a button that cannot be used now does not light
// up when the pointer rests on it, whichever kind it would have been. It is measured in the
// frame: the button's padding stays the color of the bar.
func TestGrayedButtonsStayGrayUnderThePointer(t *testing.T) {
	for _, s := range []struct {
		name, label string
		setup       func(a *App)
	}{
		{"the next step with no address to open (ink)", "打开网页对话", func(a *App) { a.tsURL = "" }},
		{"wake before the account is read (ink)", "唤醒 G4", func(a *App) { stateIdle(a); a.acct = Account{} }},
		{"release with nothing to release (danger)", "释放", stateIdle},
		{"keep alive with nothing connected (outline)", "续时", stateIdle},
	} {
		a := fakeApp(t)
		s.setup(a)
		tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
		tt.Advance(2 * time.Second)
		r, ok := tt.Find(s.label)
		if !ok {
			t.Fatalf("%s: no %q", s.name, s.label)
		}
		x, y := r.X-8, r.Y+r.H/2 // in the button's padding, left of its label
		bar := darkPalette.Panel
		for _, hovered := range []bool{false, true} {
			if hovered {
				tt.Move(x, y)
				tt.Frame()
				tt.Advance(400 * time.Millisecond) // a hover's color has eased in by now
			}
			if c := tt.Image().RGBAAt(int(x), int(y)); c.R != bar.R || c.G != bar.G || c.B != bar.B {
				t.Errorf("%s: hovered=%v, the padding of %q is #%02X%02X%02X, not the bar's", s.name, hovered, s.label, c.R, c.G, c.B)
			}
		}
	}
}
