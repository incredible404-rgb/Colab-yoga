package main

import (
	"fmt"
	"image/png"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/egoist/mygo/ui"
)

// TestReadmeFrames writes the frames of the README's animation (docs/motion.gif): one
// session from an empty console to a model answering, with the minutes a wake and a
// download take compressed into seconds. FRAMES_DIR=... go test -run ReadmeFrames
func TestReadmeFrames(t *testing.T) {
	dir := os.Getenv("FRAMES_DIR")
	if dir == "" {
		t.Skip("set FRAMES_DIR to write the frames")
	}
	const step = 66 * time.Millisecond
	a := fakeApp(t)
	st, llm := *a.status, *a.status.LLM
	stateIdle(a)
	base := time.Now()
	var passed time.Duration
	a.now = func() time.Time { return base.Add(passed) }
	tt := ui.NewTesterWith(a.view, 1080, 675, ui.TesterOptions{Dark: true})
	frame := 0
	// play shows d of the animation while the console's own clock runs speed times as fast.
	play := func(d time.Duration, speed float64) {
		for shown := time.Duration(0); shown < d; shown += step {
			passed += time.Duration(float64(step) * speed)
			tt.Frame()
			tt.Advance(step)
			f, err := os.Create(filepath.Join(dir, fmt.Sprintf("frame_%04d.png", frame)))
			if err != nil {
				t.Fatal(err)
			}
			if err := png.Encode(f, tt.Image()); err != nil {
				t.Fatal(err)
			}
			f.Close()
			frame++
		}
	}

	play(900*time.Millisecond, 1)
	if err := tt.Click("唤醒 G4"); err != nil {
		t.Fatal(err)
	}
	for _, l := range []struct {
		line string
		d    time.Duration
	}{
		{"WAKE_LOG: 🔍 正在检查账号下已有的 Colab 实例...", 400 * time.Millisecond},
		{"WAKE_LOG: 🚀 正在向 Google Colab 申请分配 G4 实例...", 800 * time.Millisecond},
		{"WAKE_LOG: ✅ 成功分配 G4 云端虚拟机！正在等待容器内核就绪...", 900 * time.Millisecond},
		{"WAKE_LOG: 正在上传 Colab-yoga 服务端代码 (colab_yoga_server.py)...", 700 * time.Millisecond},
		{"WAKE_LOG: 正在下发控制接口令牌...", 400 * time.Millisecond},
	} {
		a.noteWake(cleanWakeLine(l.line))
		play(l.d, 30)
	}
	a.wakeStage, a.wakeLine = len(wakeStages)-1, "云端服务已就绪"
	play(400*time.Millisecond, 30)

	// The server answers: connected, nothing loaded yet.
	llm.Online = false
	st.LLM, st.Uptime, st.VRAMUsed, st.IdleRemaining = &llm, 118, 0.6, 1800
	a.waking, a.serverURL = false, "https://abcdef.gradio.invalid"
	a.acct.Active, a.acct.Burn = 1, 10.05
	a.keeper = Keeper{Alive: true, Beats: 1}
	since := passed
	// report hands the console a fresh status, as a poll does, its uptime running on.
	report := func() {
		st.Uptime = 118 + (passed - since).Seconds()
		a.applyStatus(&st)
	}
	report()
	play(1100*time.Millisecond, 1)

	// A model is asked for: it downloads, loads into VRAM, and answers.
	a.switching, a.switchAt = "gemma4-31b", a.now()
	st.IsDownloading = true
	for p := 0.08; p < 1; p += 0.13 {
		st.TaskProgress = p
		st.TaskMessage = fmt.Sprintf("并发下载进度: %.0f%% (%.1f / 34.8 GiB)，均速 3135 MB/s", p*100, p*34.8)
		report()
		play(230*time.Millisecond, 8)
	}
	st.IsDownloading, st.IsLoading, st.TaskProgress, st.TaskMessage = false, true, 0, "正在把权重装入显存…"
	for _, used := range []float64{9.5, 24.1, 41.7} {
		st.VRAMUsed = used
		report()
		play(300*time.Millisecond, 8)
	}
	llm.Online = true
	st.IsLoading, st.TaskMessage, st.VRAMUsed = false, "", 52.4
	a.access.Llama = LlamaProbe{OK: true, Alias: llm.Alias, NCtx: llm.NCtx, SlotsTotal: 1}
	report() // clears switching and says the model is ready
	play(1700*time.Millisecond, 1)

	if err := tt.Click("切换到浅色"); err != nil {
		t.Fatal(err)
	}
	play(1500*time.Millisecond, 1)
	t.Logf("%d frames of %v in %s", frame, step, dir)
}
