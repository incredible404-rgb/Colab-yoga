package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/egoist/mygo/ui"
)

// view is the whole window: a top bar that never scrolls (status, money, the three
// actions used most), a left rail (pages + model library), the page, and a log drawer.
func (a *App) view(c *ui.Context) {
	root := ui.Column(c).Fill()
	// Switching themes crossfades every color of the palette rather than flashing.
	light := float32(0)
	if a.themeName == "light" {
		light = 1
	}
	p := blendPalette(root.AnimateTo("theme", light, fadeTheme))
	if !root.Settled("theme") {
		// The blend alone carries every color: the elements' own color easings would
		// trail it by their duration and make the switch feel twice as long.
		c.DisableAnimations()
	}
	applyTheme(c, p)
	a.dialogs(c)
	if a.toast != "" {
		c.Toast(a.toast)
		a.toast = ""
	}
	root.Background(p.Bg).Children(func() {
		a.topBar(c, p)
		body := ui.Row(c).Grow(1).AlignItems(ui.Stretch)
		// The drawer's width rides a spring and the split beside it takes the rest, so the
		// page narrows in step as the log slides in from the right edge.
		open := float32(0)
		if a.logsOpen {
			open = 1
		}
		drawer := body.AnimateTo("drawer", open, springEnter)
		body.Children(func() {
			ui.Split(c, &a.navWidth, func() { a.nav(c, p) }, func() { a.page_(c, p) }).Grow(1)
			if drawer > 0.002 {
				a.logPanel(c, p, drawer)
			}
		})
	})
}

// busy reports a job in flight, the only time the status lights pulse.
func (a *App) busy() bool {
	st := a.status
	loading := st != nil && a.connected && (st.IsLoading || st.IsDownloading)
	inferring := st != nil && a.connected && st.LLM != nil && st.LLM.SlotsBusy > 0
	recovering := !a.connected && a.acct.Active > 0
	return a.waking || a.releasing || a.discovering || a.switching != "" || loading || inferring || recovering
}

// ─── Top bar ──────────────────────────────────────────────────────────────────

func (a *App) topBar(c *ui.Context, p Palette) {
	ui.Row(c).Height(60).PaddingX(22).Gap(14).Background(p.Panel).
		BorderWidth(0, 0, 1, 0).BorderColor(p.Border).Children(func() {
		ui.Text(c, "Colab-yoga").Font(fontSerif).FontSize(22).NoWrap()
		ui.Box(c).Width(1).Height(22).Background(p.BorderStrong)
		col, text := a.runtimeLine(p)
		statusDot(c, col, 9, a.busy())
		ui.Text(c, text).SingleLine().Grow(1).Shrink(1).MinWidth(60).TextColor(p.Text2).Tooltip(text).
			ContentTransition(morph)
		if a.acct.Known && a.acct.LoggedIn {
			rate, _, _ := a.burnRate(a.now())
			money := ui.Text(c, fmt.Sprintf("余额 %s CU · %.2f CU/h", thousands(a.balance()), rate)).
				Font(fontMono).FontFeatures("tnum").FontSize(13).NoWrap().ContentTransition(numericDown)
			money.TextColor(money.AnimateColor("c", map[bool]ui.Color{true: p.Accent, false: p.Text2}[a.acct.Burn > 0], fadeColor))
		}
		inkButton(c, p, "打开网页对话").Disabled(a.tsURL == "").OnClick(a.openWeb)
		outlineButton(c, p, "续时").Disabled(!a.connected).Tooltip("重置闲置自动释放倒计时").OnClick(a.touch)
		dangerButton(c, p, "释放").Disabled(a.releasing || (!a.connected && a.acct.Active == 0)).
			OnClick(func() { a.askRelease = true })
		logs := ghostButton(c, p, "日志").OnClick(func() {
			a.logsOpen = !a.logsOpen
			a.logsAlert = false
		})
		if a.logsAlert {
			logs.Children(func() { dot(c, p.Danger, 7).Absolute().Top(4).Right(4).Transition(popIn) })
		}
	})
}

// runtimeLine summarizes the runtime in one line for the top bar.
func (a *App) runtimeLine(p Palette) (ui.Color, string) {
	region := ""
	if code, name := regionOf(a.acct.Endpoint); code != "" {
		region = " · " + strings.TrimSpace(name+" "+code)
	}
	gpu := a.acct.Accel
	if st := a.status; st != nil && st.GPUName != "" && a.connected {
		gpu = strings.TrimSpace(gpu + " · " + shortGPU(st.GPUName))
	}
	switch {
	case a.releasing:
		return p.Warning, "正在释放实例…"
	case a.waking:
		return p.Warning, "正在唤醒并部署…"
	case a.connected:
		return p.Success, strings.TrimPrefix(gpu+region+" · 在线", " · ")
	case a.acct.Active > 0:
		return p.Warning, strings.TrimPrefix(gpu+region+" · 实例在计费，正在找回服务地址", " · ")
	case !a.acct.Known:
		return p.Muted, "正在查询 Colab 账号…"
	case !a.acct.LoggedIn:
		return p.Muted, "未绑定 Google 账号"
	}
	return p.Muted, "没有运行中的实例，不在计费"
}

func shortGPU(name string) string {
	name = strings.ReplaceAll(name, "NVIDIA ", "")
	name = strings.ReplaceAll(name, " Blackwell Server Edition", "")
	return name
}

// ─── Left rail ────────────────────────────────────────────────────────────────

func (a *App) nav(c *ui.Context, p Palette) {
	ui.Scroll(c).Fill().Background(p.Panel).Padding(16, 14).Gap(4).Children(func() {
		a.navItem(c, p, "dashboard", "仪表盘", "算力 · 模型 · 接入")
		a.navItem(c, p, "connection", "连接与账号", "Google 账号 · 唤醒 · 自动释放")

		ui.Row(c).Margin(18, 4, 6, 4).Children(func() {
			ui.Text(c, "模型").Font(fontSerif).FontSize(18).Grow(1)
			if st := a.status; st != nil && st.VRAMTotal > 0 && a.connected {
				ui.Text(c, fmt.Sprintf("显存 %.0f GB", st.VRAMTotal)).FontSize(12).TextColor(p.Text3)
			}
		})
		served, _, online := a.servedModel()
		for i := range a.catalog {
			a.modelRow(c, p, &a.catalog[i], online && served != "" && served == a.catalog[i].ID, i)
		}
		if len(a.catalog) == 0 {
			ui.Text(c.Key("no-models"), noModelsHint).FontSize(12).TextColor(p.Text3).Margin(0, 4, 6, 4)
		}

		ui.Spacer(c).MinHeight(20)
		themeIdx := 0
		if a.themeName == "light" {
			themeIdx = 1
		}
		if ui.Segmented(c, &themeIdx, "深色", "浅色").Label("配色").Changed() {
			a.setTheme(map[int]string{0: "dark", 1: "light"}[themeIdx])
		}
	})
}

func (a *App) navItem(c *ui.Context, p Palette, page, title, sub string) {
	a.navRow(c, p, title, sub, a.page == page).OnClick(func() { a.page = page })
}

// navHighlight is the identity of the active page's highlight: one element that, as the
// page changes, flies from the row it left to the one chosen (a shared-element
// transition, as motion.dev's layoutId), rather than one fading out as another fades in.
type navHighlight struct{}

func (a *App) navRow(c *ui.Context, p Palette, title, sub string, active bool) ui.Element {
	row := ui.Box(c.Key(title)).Padding(10, 12).Radius(10).Gap(2).Cursor(ui.CursorPointer).Role(ui.RoleButton).Label(title)
	if !active && row.Hovered() {
		row.Background(p.Raised.Alpha(0.55))
	} else {
		row.Background(p.Raised.Alpha(0))
	}
	press := ui.Pose{}
	if row.Pressed() {
		press.Scale = 0.98
	}
	row.Transition(colorMotion).AnimatePose(press, springLift)
	return row.Children(func() {
		if active {
			ui.Box(c.Key("highlight")).Absolute().Fill().ZIndex(-1).IgnoreInput(true).Radius(10).Background(p.Raised).
				Transition(ui.ElementTransition{Match: navHighlight{}, Animation: &springPop}).Children(func() {
				ui.Box(c).Absolute().Left(0).Top(9).Bottom(9).Width(3).Radius(2).Background(p.Accent)
			})
		}
		t := ui.Text(c, title).FontSize(14).FontWeight(map[bool]int{true: 650, false: 500}[active])
		t.TextColor(t.AnimateColor("c", map[bool]ui.Color{true: p.Text, false: p.Text2}[active], fadeColor))
		ui.Text(c, sub).FontSize(12).TextColor(p.Text3).SingleLine()
	})
}

func (a *App) modelRow(c *ui.Context, p Palette, m *Model, running bool, i int) {
	box := ui.Column(c.Key(m.ID)).Padding(10, 12).Radius(10).Gap(3).Background(p.Card)
	border := p.Border
	switch {
	case running:
		border = p.Success
	case a.switching == m.ID:
		border = p.Warning.Alpha(0.7)
	case box.Hovered():
		border = p.BorderStrong
	}
	box.Border(1.5, border).Transition(colorMotion)
	arrive(c, box, time.Duration(i+2)*stagger, true)
	box.Children(func() {
		ui.Row(c).Gap(8).Children(func() {
			ui.Text(c, m.Name).FontSize(13).FontWeight(600).SingleLine().Grow(1).Shrink(1).MinWidth(40).Tooltip(m.Name)
			switch {
			case running:
				pill(c, "运行中", p.Success)
			case a.switching == m.ID:
				pill(c, "加载中", p.Warning)
			default:
				outlineButton(c.Key("switch-"+m.ID), p, "切换").Padding(3, 10).FontSize(12).
					Label("切换到 "+m.Name).Disabled(!a.connected || a.switching != "").
					OnClick(func() { a.switchTarget, a.askSwitch = m.ID, true })
			}
		})
		tags := m.Tags
		if len(tags) > 2 {
			tags = tags[:2]
		}
		if line := modelSummary(tags, m.SizeGB); line != "" {
			ui.Text(c, line).FontSize(12).TextColor(p.Text3).SingleLine()
		}
		if a.switching == m.ID {
			// A shimmer along the row's foot while its model downloads and loads.
			ui.Box(c.Key("loading")).Height(2).FillWidth().Margin(4, 0, 0, 0).Transition(rowMotion).
				Draw(func(pt *ui.Painter, r ui.Rect) { sweep(pt, r, p.Warning, 1) })
		}
	})
	box.Margin(0, 0, 6, 0)
}

// ─── Page ─────────────────────────────────────────────────────────────────────

func (a *App) page_(c *ui.Context, p Palette) {
	ui.Scroll(c).Fill().Children(func() {
		// Keyed by page: the page left lifts and fades out over the next, whose header and
		// cards rise in one after another (arrive).
		ui.Column(c.Key("page-"+a.page)).FillWidth().MaxWidth(1240).Margin(0, ui.Auto).Padding(28, 32, 36, 32).Gap(22).
			Transition(pageMotion).Children(func() {
			switch a.page {
			case "connection":
				a.connectionPage(c, p)
			default:
				a.dashboard(c, p)
			}
		})
	})
}

// pageHeader is a page's title and subtitle, the first to arrive when the page shows.
func pageHeader(c *ui.Context, p Palette, title, sub string) {
	h := ui.Column(c.Key("header")).Gap(6)
	arrive(c, h, 0, false)
	h.Children(func() {
		ui.Text(c, title).Font(fontSerif).FontSize(34)
		ui.Text(c, sub).FontSize(14).TextColor(p.Text3).ContentTransition(numeric)
	})
}

// ─── Log drawer ───────────────────────────────────────────────────────────────

func (a *App) logPanel(c *ui.Context, p Palette, shown float32) {
	const w = 400
	ui.Box(c.Key("logs")).Width(w*max(shown, 0)).ClipX().Background(p.Panel).
		BorderWidth(0, 0, 0, 1).BorderColor(p.Border).Children(func() {
		a.logBody(c, p).Absolute().Top(0).Bottom(0).Right(0).Width(w).Opacity(min(1, shown*1.4))
	})
}

func (a *App) logBody(c *ui.Context, p Palette) ui.Element {
	return ui.Column(c).Padding(16).Gap(10).Children(func() {
		ui.Row(c).Gap(8).Children(func() {
			ui.Text(c, "运行日志").Font(fontSerif).FontSize(18).Grow(1)
			ghostButton(c, p, "复制").OnClick(func() {
				a.copy(strings.Join(append(append([]string{}, a.serverLog...), a.logs...), "\n"), "已复制日志")
			})
			ghostButton(c, p, "清空").OnClick(func() { a.logs, a.serverLog = nil, nil })
			ghostButton(c, p, "收起").OnClick(func() { a.logsOpen = false })
		})
		ui.Scroll(c).Grow(1).Padding(12).Radius(10).Background(p.Card).Border(1, p.Border).Gap(3).Children(func() {
			lines := a.serverLog
			if len(lines) > 120 {
				lines = lines[len(lines)-120:]
			}
			if len(lines) > 0 {
				ui.Text(c, "云端服务端").FontSize(12).TextColor(p.Text3)
			}
			for _, l := range lines {
				ui.Text(c, l).Font(fontMono).FontSize(12).TextColor(p.Text2)
			}
			ui.Text(c, "本机").FontSize(12).TextColor(p.Text3).Margin(8, 0, 0, 0)
			for _, l := range a.logs {
				ui.Text(c, l).Font(fontMono).FontSize(12).TextColor(p.Text2)
			}
		})
	})
}

// ─── Confirmations ────────────────────────────────────────────────────────────

func (a *App) dialogs(c *ui.Context) {
	if ui.AlertDialog(c, &a.askRelease, "释放全部 Colab 实例？",
		"会立即停止扣费；云端的模型、网页对话、Magpie 和 MCP 都会断开。", "取消", "释放") == 1 {
		a.release()
	}
	curID, cur, _ := a.servedModel()
	if m := a.modelByID(curID); m != nil {
		cur = m.Name
	}
	msg := "下载并装载要几分钟。"
	if cur != "" {
		msg = "云端正在运行 " + cur + "，切换会先停掉它：网页、Magpie 和 MCP 里正在进行的对话会中断。新模型下载并装载要几分钟。"
	}
	if ui.AlertDialog(c, &a.askSwitch, "切换到 "+a.modelName(a.switchTarget)+"？", msg, "取消", "切换") == 1 {
		a.switchTo(a.switchTarget)
	}
	if ui.AlertDialog(c, &a.askLogout, "退出 Google 账号？",
		"退出后查不到账本、也不能唤醒或释放实例；已在运行的实例会继续计费。", "取消", "退出") == 1 {
		a.logout()
	}
	id, _, _ := a.servedModel()
	if ui.AlertDialog(c, &a.askUnload, "卸载 "+a.modelName(id)+"？",
		"释放显存，网页对话会断开；GPU 仍在计费，不用了请点「释放」。", "取消", "卸载") == 1 {
		a.unload(id)
	}
}

// ─── Small pieces in the Anthropic look ───────────────────────────────────────

// pill is a status badge. Its tint eases to a new state's color, its width springs to
// fit the new word, and the word crossfades with a short blur.
func pill(c *ui.Context, text string, col ui.Color) ui.Element {
	b := ui.Box(c).Padding(3, 10).Radius(999).Background(col.Alpha(0.16)).
		Transition(ui.ElementTransition{Colors: true, Width: true, Animation: &springSize})
	return b.Children(func() {
		t := ui.Text(c, text).FontSize(12).FontWeight(600).NoWrap().ContentTransition(crossfade)
		t.TextColor(t.AnimateColor("c", col, fadeColor))
	})
}

func dot(c *ui.Context, col ui.Color, size float32) ui.Element {
	return statusDot(c, col, size, false)
}

// styleButton gives a button its look and its feel: colors ease on hover, and a press
// sinks it slightly on a spring that bounces back on release.
func styleButton(b ui.Element, bg, hover, fg, border ui.Color) ui.Element {
	b.Padding(7, 16).Radius(8).TextColor(fg).Border(1, border).FontSize(13).FontWeight(600)
	if b.Hovered() && !b.IsDisabled() {
		b.Background(hover)
	} else {
		b.Background(bg)
	}
	press := ui.Pose{}
	if b.Pressed() && !b.IsDisabled() {
		press.Scale = 0.96
	}
	return b.Transition(colorMotion).AnimatePose(press, springLift)
}

func inkButton(c *ui.Context, p Palette, label string) ui.Element {
	return styleButton(ui.Button(c, label), p.Ink, p.InkHover, p.InkText, p.Ink)
}

func outlineButton(c *ui.Context, p Palette, label string) ui.Element {
	return styleButton(ui.Button(c, label), ui.Transparent, p.Raised, p.Text, p.BorderStrong)
}

func ghostButton(c *ui.Context, p Palette, label string) ui.Element {
	return styleButton(ui.Button(c, label), ui.Transparent, p.Raised, p.Text2, ui.Transparent)
}

func dangerButton(c *ui.Context, p Palette, label string) ui.Element {
	return styleButton(ui.Button(c, label), ui.Transparent, p.Danger.Alpha(0.14), p.Danger, p.Danger.Alpha(0.55))
}

func accentButton(c *ui.Context, p Palette, label string) ui.Element {
	return styleButton(ui.Button(c, label), p.Accent, p.AccentHover, p.AccentText, p.Accent)
}

// card is the page's unit: a serif title, a quiet subtitle, an optional badge. It rises
// in order (nth) when its page shows, and its border brightens under the pointer.
func card(c *ui.Context, p Palette, nth int, title, sub string, badge func(), body func()) ui.Element {
	box := ui.Column(c.Key(title)).Padding(24).Gap(18).Radius(16).Background(p.Card)
	border := p.Border
	if box.Hovered() {
		border = p.BorderStrong
	}
	box.Border(1, border).Transition(colorMotion)
	arrive(c, box, time.Duration(nth)*stagger, false)
	return box.Children(func() {
		ui.Row(c).Gap(12).AlignItems(ui.Start).Children(func() {
			ui.Column(c).Grow(1).Shrink(1).Gap(4).Children(func() {
				ui.Text(c, title).Font(fontSerif).FontSize(22)
				if sub != "" {
					ui.Text(c, sub).FontSize(13).TextColor(p.Text3)
				}
			})
			if badge != nil {
				badge()
			}
		})
		body()
	})
}

func metric(c *ui.Context, p Palette, label, value, sub string, size float32, col ui.Color) ui.Element {
	return metricRoll(c, p, label, value, sub, size, col, crossfade)
}

// metricRoll is a metric whose digits roll to each new value, as an odometer: up for
// what grows (time, money spent), down (numericDown) for what runs out.
func metricRoll(c *ui.Context, p Palette, label, value, sub string, size float32, col ui.Color, roll ui.ContentTransition) ui.Element {
	return ui.Column(c).Gap(3).Children(func() {
		ui.Text(c, label).FontSize(12).TextColor(p.Text3).NoWrap()
		v := ui.Text(c, value).Font(fontMono).FontFeatures("tnum").FontSize(size).NoWrap().ContentTransition(roll)
		v.TextColor(v.AnimateColor("c", col, fadeColor))
		if sub != "" {
			ui.Text(c, sub).FontSize(12).TextColor(p.Text3)
		}
	})
}

func thousands(v float64) string {
	s := fmt.Sprintf("%.2f", v)
	intPart, frac, _ := strings.Cut(s, ".")
	neg := strings.HasPrefix(intPart, "-")
	intPart = strings.TrimPrefix(intPart, "-")
	var b strings.Builder
	for i, r := range intPart {
		if i > 0 && (len(intPart)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	out := b.String() + "." + frac
	if neg {
		out = "-" + out
	}
	return out
}

func hms(sec float64) string {
	d := time.Duration(sec) * time.Second
	return fmt.Sprintf("%02d:%02d:%02d", int(d.Hours()), int(d.Minutes())%60, int(d.Seconds())%60)
}

func ago(sec int) string {
	switch {
	case sec < 0:
		return "—"
	case sec < 60:
		return "刚刚"
	case sec < 3600:
		return fmt.Sprintf("%d 分钟前", sec/60)
	}
	return fmt.Sprintf("%d 小时前", sec/3600)
}

// modelLabel is a model's line in the switch menu.
func modelLabel(m *Model) string {
	if m.SizeGB <= 0 {
		return m.Name
	}
	return fmt.Sprintf("%s  ·  %.0f GB", m.Name, m.SizeGB)
}

// modelSummary is the small line under a model's name in the left rail.
func modelSummary(tags []string, sizeGB float64) string {
	parts := append([]string{}, tags...)
	if sizeGB > 0 {
		parts = append(parts, fmt.Sprintf("%.0f GB", sizeGB))
	}
	return strings.Join(parts, " · ")
}
