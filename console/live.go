package main

import (
	"fmt"
	"strconv"
	"strings"
	"unicode"

	"github.com/egoist/mygo/ui"
)

// Icons are drawn in the color of the text around them (ui.Icon).
var (
	iconMoon = ui.MustParseSVG([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`))
	iconSun  = ui.MustParseSVG([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.4"/><path d="M12 1.6v3M12 19.4v3M1.6 12h3M19.4 12h3M4.65 4.65l2.1 2.1M17.25 17.25l2.1 2.1M4.65 19.35l2.1-2.1M17.25 6.75l2.1-2.1" fill="none" stroke="#000" stroke-width="2.4" stroke-linecap="round"/></svg>`))
	iconMore = ui.MustParseSVG([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>`))
)

// ─── Stages of a wake and of a model load ─────────────────────────────────────

// A wake shows as four stages and a model load as three. The bridge reports a wake only as
// log lines, so its stage is read from their wording (wakeStageOf): a line this does not
// recognise leaves the stage where it was and still shows as the latest line.
var (
	wakeStages = []string{"申请 GPU", "启动内核", "部署服务端", "服务就绪"}
	wakeDoing  = []string{"正在申请 GPU…", "正在启动内核…", "正在部署服务端…", "服务已就绪，正在连接…"}
	loadStages = []string{"下载权重", "装入显存", "模型就绪"}
)

var wakeWords = []struct {
	stage int
	words []string
}{
	{2, []string{"上传", "下发控制接口令牌", "安装缺失依赖"}},
	{1, []string{"等待容器内核", "连接内核", "连通性"}},
	{0, []string{"检查账号下已有", "申请分配", "重新分配", "释放并切换"}},
}

// wakeStageOf is the stage a bridge log line belongs to, or -1 for a line that marks none.
func wakeStageOf(line string) int {
	for _, w := range wakeWords {
		for _, word := range w.words {
			if strings.Contains(line, word) {
				return w.stage
			}
		}
	}
	return -1
}

// cleanWakeLine is a bridge log line as the card shows it: without the WAKE_LOG: prefix
// and the emoji it opens with, its trailing dots as one ellipsis.
func cleanWakeLine(line string) string {
	s := strings.TrimSpace(strings.TrimPrefix(line, "WAKE_LOG:"))
	s = strings.TrimLeftFunc(s, func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r) && !strings.ContainsRune("[「（(", r)
	})
	if strings.HasSuffix(s, "...") {
		s = strings.TrimRight(s, ".") + "…"
	}
	return s
}

func (a *App) noteWake(line string) {
	if line == "" {
		return
	}
	a.wakeLine = line
	if s := wakeStageOf(line); s >= 0 {
		a.wakeStage = s
	}
}

// mmss is a duration in seconds as a stopwatch shows it.
func mmss(sec float64) string {
	s := int(max(0, sec))
	return fmt.Sprintf("%02d:%02d", s/60, s%60)
}

// wakeTitle names the wake in flight: a new runtime, or the server put back on one that runs.
func (a *App) wakeTitle() string {
	if a.redeploying {
		return "正在重新部署服务端"
	}
	return "正在唤醒 " + a.gpu
}

// wakeElapsed is how long the wake in flight has run.
func (a *App) wakeElapsed() string {
	if a.wakeAt.IsZero() {
		return mmss(0)
	}
	return mmss(a.now().Sub(a.wakeAt).Seconds())
}

// stageTrack draws a job's stages in a row: those done ticked, the one running ringed with
// a pulse and a band of light on the line to the next, the rest hollow. With failed, the
// current stage is where the job stopped. Pulse and band draw from Draw, which repaints
// without running the view, and only while the job runs.
func stageTrack(c *ui.Context, p Palette, key string, names []string, cur int, failed bool) ui.Element {
	return ui.Row(c.Key(key)).Gap(10).AlignItems(ui.Center).Transition(liveMotion).Children(func() {
		for i, name := range names {
			done, on := i < cur, i == cur
			ui.Row(c.Key(name)).Gap(8).AlignItems(ui.Center).Children(func() {
				stageNode(c, p, i+1, done, on, failed && on)
				col, weight := p.Text3, 500
				switch {
				case on && failed:
					col, weight = p.Danger, 650
				case on:
					col, weight = p.Text, 650
				case done:
					col = p.Text
				}
				t := ui.Text(c, name).FontSize(13).FontWeight(weight).NoWrap()
				t.TextColor(t.AnimateColor("c", col, fadeColor))
			})
			if i == len(names)-1 {
				continue
			}
			line := ui.Box(c.Key(name + "-line")).Grow(1).Shrink(1).MinWidth(10).Height(2)
			col := p.BorderStrong
			if done {
				col = p.Success
			}
			lc := line.AnimateColor("c", col, fadeColor)
			flow, band := on && !failed, p.Warning
			line.Draw(func(pt *ui.Painter, r ui.Rect) {
				pt.Fill(r, lc, 1)
				if flow {
					sweep(pt, r, band, 1)
				}
			})
		}
	})
}

// tickMark draws the tick of what is done. (Painter.Line draws only level and upright
// lines; what slants is a polyline.)
func tickMark(c *ui.Context, col ui.Color) ui.Element {
	return ui.Box(c).Size(12, 12).Draw(func(pt *ui.Painter, r ui.Rect) {
		pt.StrokePolyline([][2]float32{{r.X + 1.8, r.Y + 6.4}, {r.X + 4.8, r.Y + 9.4}, {r.X + 10.4, r.Y + 2.8}}, 1.8, col)
	})
}

func stageNode(c *ui.Context, p Palette, n int, done, on, failed bool) {
	const d = 22
	bg, border, fg, width := ui.Transparent, p.BorderStrong, p.Muted, float32(1)
	switch {
	case failed:
		border, fg, width = p.Danger, p.Danger, 2
	case on:
		border, fg, width = p.Warning, p.Warning, 2
	case done:
		bg, border, fg = p.Success.Alpha(0.18), ui.Transparent, p.Success
	}
	node := ui.Box(c).Size(d, d).Radius(d/2).Justify(ui.Center).AlignItems(ui.Center).
		Background(bg).Border(width, border).Transition(colorMotion)
	node.Children(func() {
		if on && !failed {
			ring := p.Warning
			ui.Box(c.Key("pulse")).Absolute().Fill().IgnoreInput(true).Draw(func(pt *ui.Painter, r ui.Rect) {
				if pt.Preferences().ReduceMotion {
					return
				}
				pt.AnimationFrameRate(30)
				e := ui.EaseOut(float32(pt.Now().UnixMilli()%1600) / 1600)
				rr := r.W/2 + e*9
				cx, cy := r.X+r.W/2, r.Y+r.H/2
				pt.Fill(ui.Rect{X: cx - rr, Y: cy - rr, W: 2 * rr, H: 2 * rr}, ring.Alpha(0.34*(1-e)), rr)
			})
		}
		switch {
		case done:
			tickMark(c.Key("tick"), fg)
		case failed:
			ui.Text(c.Key("mark"), "!").FontSize(13).FontWeight(700).TextColor(fg)
		default:
			ui.Text(c.Key("mark"), strconv.Itoa(n)).FontSize(12).FontWeight(600).TextColor(fg)
		}
	})
}

// ─── Sign-in ──────────────────────────────────────────────────────────────────

// signingIn: the sign-in page takes the whole window while no account is bound and no
// server is connected that the console could still manage, and while an authorization
// code is awaited (the first binding, or the account menu's 换号).
func (a *App) signingIn() bool {
	return a.binding || (a.acct.Known && !a.acct.LoggedIn && !a.connected && !a.skipSignIn)
}

// signInPage is all the window shows until a Google account is bound. Binding is done
// once and then holds, so it has a page of its own instead of a place in the console.
func (a *App) signInPage(c *ui.Context, p Palette) {
	ui.Row(c).Height(60).PaddingX(22).Gap(14).Background(p.Panel).
		BorderWidth(0, 0, 1, 0).BorderColor(p.Border).Children(func() {
		ui.Text(c, "Colab-yoga").Font(fontSerif).FontSize(22).NoWrap()
		ui.Spacer(c)
		a.themeButton(c, p)
	})
	ui.Column(c).Grow(1).Justify(ui.Center).AlignItems(ui.Center).Padding(24).Children(func() {
		step := "start"
		if a.binding {
			step = "code"
		}
		ui.Column(c.Key("signin-" + step)).Width(440).Gap(14).AlignItems(ui.Start).Transition(pageMotion).Children(func() {
			if a.binding {
				a.signInCode(c, p)
				return
			}
			ui.Text(c, "登录 Google 账号").Font(fontSerif).FontSize(34)
			ui.Text(c, "用来申请 Colab GPU 和查官方账本的余额。只需要做一次，之后会一直保持登录。").FontSize(14).TextColor(p.Text2)
			label, off := "用 Google 账号登录", ""
			if a.authBusy {
				label, off = "正在打开授权页…", "正在生成授权链接"
			}
			act(inkButton(c.Key("signin"), p, label).Margin(6, 0, 0, 0), p, off, a.startBinding)
			ui.Text(c, "会在浏览器打开 Google 授权页，授权后把页面上的授权码粘贴回来。").FontSize(12).TextColor(p.Text3)
			// Signing in can be passed by: the log and the server address are worth reaching
			// most when the authorization itself is what fails.
			skip := ui.Button(c.Key("signin-skip"), "先不登录，进入控制台").Padding(4, 0).Radius(4).
				Background(ui.Transparent).Border(1, ui.Transparent).FontSize(12).
				Tooltip("不登录也能看日志、手填服务地址；唤醒和查余额要先登录")
			col := p.Text3
			if skip.Hovered() {
				col = p.Text
			}
			skip.TextColor(skip.AnimateColor("c", col, fadeColor)).OnClick(func() { a.skipSignIn = true })
		})
	})
}

func (a *App) signInCode(c *ui.Context, p Palette) {
	ui.Text(c, "粘贴授权码").Font(fontSerif).FontSize(34)
	ui.Text(c, "在浏览器里完成授权，把页面上显示的授权码粘贴到这里。").FontSize(14).TextColor(p.Text2)
	ui.Row(c).Gap(8).FillWidth().Margin(6, 0, 0, 0).Children(func() {
		in := ui.TextInput(c.Key("auth-code"), &a.authCode).Placeholder("授权码").Password().
			Font(fontMono).FontSize(13).Grow(1).Shrink(1).MinWidth(120).Label("授权码")
		if in.Submitted() {
			a.submitAuthCode()
		}
		off := ""
		switch {
		case a.authBusy:
			off = "正在处理…"
		case strings.TrimSpace(a.authCode) == "":
			off = "先粘贴授权码"
		}
		act(inkButton(c.Key("auth-submit"), p, "确认"), p, off, a.submitAuthCode)
	})
	ui.Row(c).Gap(8).Children(func() {
		off := ""
		if a.authBusy {
			off = "正在处理…"
		}
		act(outlineButton(c.Key("auth-reopen"), p, "重新打开授权页"), p, off, a.startBinding)
		ghostButton(c.Key("auth-cancel"), p, "取消").OnClick(func() { a.binding, a.authCode = false, "" })
	})
}
