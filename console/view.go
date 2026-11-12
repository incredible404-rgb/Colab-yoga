package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/egoist/mygo/ui"
)

// view is the whole window: a top bar that never scrolls (status and the session's
// steps), a left rail (pages, model library, account), the page, and a log
// drawer. Until a Google account is bound it is the sign-in page alone.
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
		if a.toastUndo != nil {
			c.ToastAction(a.toast, "撤销", a.toastUndo)
		} else {
			c.Toast(a.toast)
		}
		a.toast, a.toastUndo = "", nil
	}
	root.Background(p.Bg).Children(func() {
		if a.signingIn() {
			a.signInPage(c, p)
			return
		}
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

// loadingModel reports a model being downloaded or loaded on :8081.
func (a *App) loadingModel() bool {
	st := a.status
	return a.switching != "" || (st != nil && a.connected && (st.IsLoading || st.IsDownloading))
}

// busy reports a job in flight, the only time the status lights pulse.
func (a *App) busy() bool {
	st := a.status
	inferring := st != nil && a.connected && st.LLM != nil && st.LLM.SlotsBusy > 0
	recovering := !a.connected && a.acct.Active > 0
	return a.waking || a.releasing || a.discovering || a.loadingModel() || inferring || recovering
}

// ─── Top bar ──────────────────────────────────────────────────────────────────

// The buttons follow a session from left to right: wake, load a model, chat, keep alive,
// release. At any time the one filled button is the step that comes next; a step that is
// done keeps its place, dimmed and ticked; what cannot be used now is gray and says why
// under the pointer. Money is not here: the bill card and the tray's tooltip show it.
func (a *App) topBar(c *ui.Context, p Palette) {
	ui.Row(c).Height(60).PaddingX(22).Gap(14).Background(p.Panel).
		BorderWidth(0, 0, 1, 0).BorderColor(p.Border).Children(func() {
		ui.Text(c, "Colab-yoga").Font(fontSerif).FontSize(22).NoWrap()
		ui.Box(c).Width(1).Height(22).Background(p.BorderStrong)
		col, text, tip := a.runtimeLine(p)
		statusDot(c, col, 9, a.busy())
		ui.Text(c, text).SingleLine().Grow(1).Shrink(1).MinWidth(60).TextColor(p.Text2).Tooltip(tip).
			ContentTransition(morph)
		if a.waking {
			ui.Text(c.Key("wake-clock"), a.wakeElapsed()).Font(fontMono).FontFeatures("tnum").FontSize(13).NoWrap().
				TextColor(p.Text3).ContentTransition(numeric)
		}
		a.wakeSlot(c, p)
		a.loadSlot(c, p)
		a.chatSlot(c, p)
		a.keepButton(c, p)
		off := ""
		switch {
		case a.releasing:
			off = "正在释放实例…"
		case !a.connected && a.acct.Active == 0 && !a.waking:
			off = "没有实例可释放"
		}
		act(dangerButton(c.Key("release"), p, "释放"), p, off, func() { a.askRelease = true })
		logs := ghostButton(c, p, "日志").OnClick(func() {
			a.logsOpen = !a.logsOpen
			a.logsAlert = false
		})
		if a.logsAlert {
			logs.Children(func() { dot(c, p.Danger, 7).Absolute().Top(4).Right(4).Transition(popIn) })
		}
	})
}

// runtimeLine summarizes the runtime in one line for the top bar, with the longer text
// its tooltip shows.
func (a *App) runtimeLine(p Palette) (col ui.Color, text, tip string) {
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
		text = "正在释放实例…"
		return p.Warning, text, text
	case a.waking:
		text, tip = wakeDoing[min(max(a.wakeStage, 0), len(wakeDoing)-1)], a.wakeLine
		if tip == "" {
			tip = text
		}
		return p.Warning, text, tip
	// The state comes first: in a narrow window the line is cut at its end, which then
	// costs the region or the GPU's name rather than the word that matters.
	case a.connected:
		text = "在线" + sep(gpu) + region
		return p.Success, text, text
	case a.acct.Active > 0:
		text = "实例在计费，正在找回服务地址" + sep(gpu) + region
		return p.Warning, text, text
	case !a.acct.Known:
		text = "正在查询 Colab 账号…"
		return p.Muted, text, text
	}
	return p.Muted, "没有实例 · 不计费", "账号下没有运行中的 Colab 实例，不在计费"
}

// sep joins s to what stands before it in the status line.
func sep(s string) string {
	if s == "" {
		return ""
	}
	return " · " + s
}

func shortGPU(name string) string {
	name = strings.ReplaceAll(name, "NVIDIA ", "")
	name = strings.ReplaceAll(name, " Blackwell Server Edition", "")
	return name
}

// wakeOff says why a wake cannot start now, or "" when it can.
func (a *App) wakeOff() string {
	switch {
	case a.releasing:
		return "正在释放实例，完成后再唤醒"
	case !a.acct.Known:
		return "正在查询 Colab 账号…"
	case !a.acct.LoggedIn:
		return "先登录 Google 账号"
	}
	return ""
}

// wakeSlot holds the first step of every session: a split button that wakes the
// remembered GPU (its arrow picks another), the wake in flight, or a redeploy when a
// runtime bills without its server. With a server connected the step is done: it keeps
// its place, dimmed and ticked.
func (a *App) wakeSlot(c *ui.Context, p Palette) {
	ui.Row(c.Key("wake-slot")).Gap(1).MinWidth(104).Children(func() {
		off := a.wakeOff()
		switch {
		case a.waking:
			label := "唤醒中…"
			if a.redeploying {
				label = "部署中…"
			}
			busyButton(c.Key("waking"), p, label).Grow(1)
		case a.connected:
			doneStep(c.Key("woken"), p, "已唤醒").Grow(1).
				Tooltip("实例在运行。要重启服务端，去「连接」页点「重新部署服务端」")
		case a.acct.Active > 0:
			act(warnButton(c.Key("redeploy"), p, "重新部署").Grow(1).
				Tooltip("实例在计费但服务端没连上：复用这台机器重启服务端，:8081 上的模型不受影响"), p, off, a.wake)
		default:
			// Filled only as the next step: a model already answering on the tailnet makes
			// the chat the next step and this a plain button.
			button := inkButton
			if _, _, online := a.servedModel(); online {
				button = outlineButton
			}
			act(button(c.Key("wake"), p, "唤醒 "+a.gpu).Grow(1).Radius(8, 0, 0, 8).Padding(7, 12, 7, 16), p, off, a.wake)
			arrow := button(c.Key("wake-gpu"), p, "▾").Radius(0, 8, 8, 0).Padding(7, 9).Label("选择 GPU 型号")
			if off != "" {
				act(arrow, p, off, nil)
				return
			}
			arrow.Tooltip("选择唤醒时申请的 GPU 型号").Menu(func(m *ui.Menu) {
				for _, g := range gpuChoices {
					if m.Item(g).Radio(g == a.gpu).Chosen() {
						a.setGPU(g)
					}
				}
			})
		}
	})
}

// loadSlot holds the second step: choose a model and load it (loadPicker), then the load
// in flight. Once a model serves the step is done and keeps its place, as the wake does.
func (a *App) loadSlot(c *ui.Context, p Palette) {
	const w = 116
	id, alias, online := a.servedModel()
	picking := a.connected && !online && !a.loadingModel() && len(a.catalog) > 0
	if !picking {
		a.pickOpen = false // the library does not spring open again when the step comes back
	}
	switch {
	case a.loadingModel():
		label := "装载中…"
		if st := a.status; st != nil && a.connected && st.TaskProgress > 0 {
			label = fmt.Sprintf("装载中 %.0f%%", clamp01(st.TaskProgress)*100)
		}
		busyButton(c.Key("loading"), p, label).MinWidth(w)
	case online:
		name := alias
		if m := a.modelByID(id); m != nil {
			name = m.Name
		}
		doneStep(c.Key("loaded"), p, "已加载").MinWidth(w).
			Tooltip("正在运行 " + name + "。换模型用左栏，或「当前模型」卡的「切换模型」")
	case picking:
		a.loadPicker(c, p, w)
	case a.connected:
		act(inkButton(c.Key("load"), p, "加载模型").MinWidth(w), p, "模型库是空的："+noModelsHint, nil)
	default:
		off := "先唤醒实例"
		switch {
		case a.waking:
			off = "唤醒完成后可用"
		case a.acct.Active > 0:
			off = "云端服务端没连上：找回地址或重新部署后可用"
		}
		act(outlineButton(c.Key("load"), p, "加载模型").MinWidth(w), p, off, nil)
	}
}

// loadPicker is the load button with nothing loaded yet. A first model is chosen every
// session and is most often the one that ran last, so the first click opens the library
// with that model chosen and turns the button into the confirmation: two clicks on the
// same spot load what ran before. Choosing another model in the library is one click
// more, and a click elsewhere backs out. Nothing runs yet for a load to interrupt, so
// there is no dialog.
func (a *App) loadPicker(c *ui.Context, p Palette, w float32) {
	if a.pickOpen && a.modelByID(a.pickID) == nil {
		a.pickID = a.defaultPick() // the library changed under the open list
	}
	label := "加载模型"
	if a.pickOpen {
		label = "加载所选"
		if a.pickID == a.lastModel {
			label = "加载上次使用"
		}
	}
	b := inkButton(c.Key("load"), p, label).MinWidth(w).OnClick(func() {
		if !a.pickOpen {
			a.pickID, a.pickOpen = a.defaultPick(), true
			return
		}
		a.pickOpen = false
		a.switchTo(a.pickID)
	})
	if !a.pickOpen {
		b.Tooltip("选择要加载的模型，默认是上次用的那个")
	}
	ui.Popover(c, b, &a.pickOpen, func() {
		ui.Column(c).Width(400).Padding(8).Gap(2).Children(func() {
			for i := range a.catalog {
				a.pickRow(c, p, &a.catalog[i])
			}
			ui.Text(c, "再点一次顶栏的按钮开始加载；下载并装载要几分钟。").FontSize(12).TextColor(p.Text3).Pretty().
				Margin(6, 10, 4, 10)
		})
	})
}

// pickRow is a model in the load button's library: a radio row that chooses it.
func (a *App) pickRow(c *ui.Context, p Palette, m *Model) {
	on := m.ID == a.pickID
	row := ui.Row(c.Key("pick-"+m.ID)).Padding(8, 10).Radius(8).Gap(10).AlignItems(ui.Center).
		Cursor(ui.CursorPointer).Role(ui.RoleButton).Label("选择 " + m.Name)
	bg := p.Raised.Alpha(0)
	switch {
	case on:
		bg = p.Raised
	case row.Hovered():
		bg = p.Raised.Alpha(0.55)
	}
	id := m.ID
	row.Background(bg).Transition(colorMotion).OnClick(func() { a.pickID = id }).Children(func() {
		ring := p.BorderStrong
		if on {
			ring = p.Accent
		}
		ui.Box(c).Size(14, 14).Radius(7).Border(1.5, ring).Justify(ui.Center).AlignItems(ui.Center).
			Transition(colorMotion).Children(func() {
			if on {
				ui.Box(c.Key("dot")).Size(6, 6).Radius(3).Background(p.Accent).Transition(popIn)
			}
		})
		ui.Text(c, m.Name).FontSize(13).FontWeight(map[bool]int{true: 650, false: 500}[on]).SingleLine().
			Grow(1).Shrink(1).MinWidth(40).Tooltip(m.Name)
		if m.ID == a.lastModel {
			ui.Text(c, "上次使用").FontSize(12).TextColor(p.Accent).NoWrap()
		}
		if m.SizeGB > 0 {
			ui.Text(c, fmt.Sprintf("%.0f GB", m.SizeGB)).Font(fontMono).FontFeatures("tnum").FontSize(12).TextColor(p.Text3).NoWrap()
		}
	})
}

// defaultPick is the model the load button offers first: the one loaded last time, else
// the first of the library.
func (a *App) defaultPick() string {
	if a.modelByID(a.lastModel) != nil {
		return a.lastModel
	}
	if len(a.catalog) > 0 {
		return a.catalog[0].ID
	}
	return ""
}

// chatSlot holds the third step: open the chat with the model that serves.
func (a *App) chatSlot(c *ui.Context, p Palette) {
	_, _, online := a.servedModel()
	off := "先唤醒实例并加载模型"
	switch {
	case online && a.tsURL == "":
		off = "没配置 Tailscale 地址：去「连接」页填写"
	case online:
		off = ""
	case a.loadingModel():
		off = "模型装载完成后可用"
	case a.connected:
		off = "先加载模型"
	case a.waking:
		off = "唤醒完成并加载模型后可用"
	case a.acct.Active > 0:
		off = "云端服务端没连上：找回地址或重新部署后可用"
	}
	button := outlineButton
	if online {
		button = inkButton
	}
	act(button(c.Key("web"), p, "打开网页对话"), p, off, a.openWeb)
}

// modelMenu lists the model library; choosing a model asks before it replaces what runs.
func (a *App) modelMenu(cur string, online bool) func(*ui.Menu) {
	return func(menu *ui.Menu) {
		for i := range a.catalog {
			mm := &a.catalog[i]
			serving := online && mm.ID == cur
			if menu.Item(modelLabel(mm)).Checked(serving).Chosen() && !serving {
				a.switchTarget, a.askSwitch = mm.ID, true
			}
		}
	}
}

// keepButton resets the idle countdown. In the last five minutes before the automatic
// release it turns to the warning color and counts down itself.
func (a *App) keepButton(c *ui.Context, p Palette) {
	const keepWidth = 90 // room for the countdown, so the bar does not shift when it starts
	label, urgent := "续时", false
	if st := a.status; st != nil && a.connected && st.IdleTimeoutMin > 0 && st.IdleRemaining >= 0 {
		if rem := int(a.idleRemainingAt(a.now())); rem <= 300 {
			label, urgent = fmt.Sprintf("续时 %d:%02d", rem/60, rem%60), true
		}
	}
	off := ""
	if !a.connected {
		off = "没有连上的实例，不需要续时"
	}
	if urgent {
		act(warnButton(c.Key("touch"), p, label).MinWidth(keepWidth).Label("续时").Tooltip("快被自动释放了：点一下重置闲置倒计时"), p, off, a.touch)
		return
	}
	act(outlineButton(c.Key("touch"), p, label).MinWidth(keepWidth).Tooltip("重置闲置自动释放倒计时"), p, off, a.touch)
}

// ─── Left rail ────────────────────────────────────────────────────────────────

// nav is the left rail: the pages and the model library scroll, and the account stays at
// its foot however long the library is.
func (a *App) nav(c *ui.Context, p Palette) {
	ui.Column(c).Fill().Background(p.Panel).Children(func() {
		a.navList(c, p)
		ui.Row(c).Padding(10, 14, 12, 14).Gap(8).AlignItems(ui.Center).BorderWidth(1, 0, 0, 0).BorderColor(p.Border).Children(func() {
			a.accountChip(c, p)
			a.themeButton(c, p)
		})
	})
}

func (a *App) navList(c *ui.Context, p Palette) {
	ui.Scroll(c).Grow(1).Padding(16, 14).Gap(4).Children(func() {
		a.navItem(c, p, "dashboard", "仪表盘", "算力 · 模型 · 接入")
		a.navItem(c, p, "connection", "连接", "实例 · 服务地址 · 自动释放")

		ui.Row(c).Margin(18, 4, 6, 4).Children(func() {
			ui.Text(c, "模型").Font(fontSerif).FontSize(18).Grow(1)
			if st := a.status; st != nil && st.VRAMTotal > 0 && a.connected {
				ui.Text(c, fmt.Sprintf("显存 %.0f GB", st.VRAMTotal)).FontSize(12).TextColor(p.Text3)
			}
		})
		served, _, online := a.servedModel()
		for i := range a.catalog {
			a.modelRow(c, p, &a.catalog[i], online && served != "" && served == a.catalog[i].ID, online, i)
		}
		if len(a.catalog) == 0 {
			ui.Text(c.Key("no-models"), noModelsHint).FontSize(12).TextColor(p.Text3).Margin(0, 4, 6, 4)
		}
	})
}

// accountChip is who is signed in. Binding is done once, so the account takes one quiet
// row at the foot of the rail; its menu holds what is done to it once in a while.
func (a *App) accountChip(c *ui.Context, p Palette) {
	switch {
	case !a.acct.Known:
		ui.Text(c.Key("acct-wait"), "正在查询账号…").FontSize(12).TextColor(p.Text3).SingleLine().Grow(1).Shrink(1).MinWidth(40)
	case !a.acct.LoggedIn:
		off := ""
		if a.authBusy {
			off = "正在生成授权链接"
		}
		act(outlineButton(c.Key("acct-signin"), p, "登录 Google 账号").Grow(1), p, off, a.startBinding)
	default:
		chip := ui.ButtonBase(c.Key("account")).Grow(1).Shrink(1).MinWidth(60).Padding(8, 10).Radius(10).
			Cursor(ui.CursorPointer).TextColor(p.Text2).Label("账号").Tooltip("账号：换号、刷新余额、退出")
		bg, border := ui.Transparent, p.Border
		if chip.Hovered() {
			bg, border = p.Raised.Alpha(0.55), p.BorderStrong
		}
		chip.Background(bg).Border(1, border).Transition(colorMotion).Children(func() {
			ui.Row(c).Gap(8).AlignItems(ui.Center).FillWidth().Children(func() {
				dot(c, p.Success, 8)
				ui.Text(c, maskEmail(a.acct.Email)).FontSize(13).TextColor(p.Text).SingleLine().Grow(1).Shrink(1).MinWidth(30)
				ui.Icon(c, iconMore).Size(16, 16)
			})
		})
		chip.Menu(func(m *ui.Menu) {
			if m.Item("换号").Disabled(a.authBusy).Chosen() {
				a.startBinding()
			}
			if m.Item("刷新余额").Chosen() {
				a.once("acct", a.refreshAccount)
			}
			m.Separator()
			if m.Item("退出账号").Danger().Chosen() {
				a.askLogout = true
			}
		})
		confirmHere(c, p, chip, &a.askLogout, "退出 Google 账号？",
			"退出后查不到账本、也不能唤醒或释放实例；已在运行的实例会继续计费。", "确认退出", true, a.logout)
	}
}

// themeButton switches between the dark and the light theme; it shows the one in use.
func (a *App) themeButton(c *ui.Context, p Palette) {
	icon, to, label := iconMoon, "light", "切换到浅色"
	if a.themeName == "light" {
		icon, to, label = iconSun, "dark", "切换到深色"
	}
	b := ui.ButtonBase(c.Key("theme")).Size(34, 34).Radius(8).Justify(ui.Center).AlignItems(ui.Center).
		Cursor(ui.CursorPointer).TextColor(p.Text2).Label(label).Tooltip(label)
	bg := ui.Transparent
	if b.Hovered() {
		bg = p.Raised
	}
	press := ui.Pose{}
	if b.Pressed() {
		press.Scale = 0.92
	}
	b.Background(bg).Transition(colorMotion).AnimatePose(press, springLift).
		Children(func() { ui.Icon(c, icon).Size(16, 16) }).
		OnClick(func() { a.setTheme(to) })
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

// modelRow is a model of the library. anyOnline says some model is being served, which
// makes loading this one a switch.
func (a *App) modelRow(c *ui.Context, p Palette, m *Model, running, anyOnline bool, i int) {
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
				verb, label := "加载", "加载 "+m.Name
				if anyOnline {
					verb, label = "切换", "切换到 "+m.Name
				}
				off := ""
				switch {
				case !a.connected:
					off = "先唤醒实例"
				case a.loadingModel():
					off = "另一个模型正在装载"
				}
				ask := ui.Local(box, "ask", func() bool { return false })
				b := act(outlineButton(c.Key("switch-"+m.ID), p, verb).Padding(3, 10).FontSize(12).Label(label), p, off,
					func() { *ask = true })
				confirmHere(c, p, b, ask, label+"？", a.switchCost(), "确认"+verb, false, func() { a.switchTo(m.ID) })
			}
		})
		tags := m.Tags
		if len(tags) > 2 {
			tags = tags[:2]
		}
		if line := modelSummary(tags, m.SizeGB); line != "" {
			ui.Text(c, line).FontSize(12).TextColor(p.Text3).SingleLine().Tooltip(line)
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
		ui.Text(c, title).Font(fontSerif).FontSize(34).Balance()
		if sub != "" {
			ui.Text(c.Key("sub"), sub).FontSize(14).FontFeatures("tnum").TextColor(p.Text3).Pretty().ContentTransition(numeric)
		}
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
			ghostButton(c, p, "清空").OnClick(a.clearLogs)
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

// Confirmations come in two sizes. Releasing cuts every way in at once and asks in a
// dialog; so does a model picked from a menu, which leaves nothing on screen to anchor
// to. What touches one thing (a model's row, unloading, signing out) asks right under
// its button (confirmHere), and what can be taken back (clearing the log) is done at once
// with an Undo.
func (a *App) dialogs(c *ui.Context) {
	if ui.AlertDialog(c, &a.askRelease, "释放全部 Colab 实例？",
		"会立即停止扣费；云端的模型、网页对话、Magpie 和 MCP 都会断开。", "取消", "释放") == 1 {
		a.release()
	}
	title, yes := "切换到 "+a.modelName(a.switchTarget)+"？", "切换"
	if _, _, online := a.servedModel(); !online {
		title, yes = "加载 "+a.modelName(a.switchTarget)+"？", "加载" // nothing runs to be switched from
	}
	if ui.AlertDialog(c, &a.askSwitch, title, a.switchCost(), "取消", yes) == 1 {
		a.switchTo(a.switchTarget)
	}
}

// switchCost says what loading another model costs: what stops, and how long it takes.
func (a *App) switchCost() string {
	id, cur, _ := a.servedModel()
	if m := a.modelByID(id); m != nil {
		cur = m.Name
	}
	if cur == "" {
		return "下载并装载要几分钟。"
	}
	return "云端正在运行 " + cur + "，切换会先停掉它：网页、Magpie 和 MCP 里正在进行的对话会中断。新模型下载并装载要几分钟。"
}

// confirmHere asks about one action in a small panel under the button that started it:
// what it costs and the two answers. A click elsewhere or Escape is a no.
func confirmHere(c *ui.Context, p Palette, anchor ui.Element, open *bool, title, cost, yes string, danger bool, do func()) {
	ui.Popover(c, anchor, open, func() {
		ui.Column(c).Width(300).Padding(14).Gap(10).Children(func() {
			ui.Text(c, title).FontSize(14).FontWeight(650).Balance()
			ui.Text(c, cost).FontSize(13).TextColor(p.Text2).Pretty()
			ui.Row(c).Gap(8).Justify(ui.End).Children(func() {
				ghostButton(c.Key("no"), p, "取消").Padding(4, 12).OnClick(func() { *open = false })
				button := outlineButton
				if danger {
					button = dangerButton
				}
				button(c.Key("yes"), p, yes).Padding(4, 12).OnClick(func() {
					*open = false
					do()
				})
			})
		})
	})
}

// clearLogs empties the log at once and offers to take it back.
func (a *App) clearLogs() {
	local, server := a.logs, a.serverLog
	a.logs, a.serverLog = nil, nil
	a.toast, a.toastAt = "已清空日志", a.now()
	a.toastUndo = func() {
		a.logs = append(local, a.logs...)
		if a.serverLog == nil { // else the next poll already brought the server's lines back
			a.serverLog = server
		}
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

// The button kinds, from loudest to quietest. ink is the one step that comes next and
// there is one of it on screen; outline is what can be done besides; ghost is a tool;
// danger and warn say what a click costs or how soon it is needed.
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

func warnButton(c *ui.Context, p Palette, label string) ui.Element {
	return styleButton(ui.Button(c, label), p.Warning.Alpha(0.14), p.Warning.Alpha(0.26), p.Warning, p.Warning.Alpha(0.55))
}

// act wires a button to fn. Given why it cannot be used now, the button is grayed out
// instead (no fill, no border, muted text) and says so under the pointer. It is not
// Disabled, which would also take the tooltip away: it stays hoverable and has no click.
func act(b ui.Element, p Palette, off string, fn func()) ui.Element {
	if off == "" {
		if fn != nil {
			b.OnClick(fn)
		}
		return b
	}
	return b.AccessDisabled(true).Cursor(ui.CursorDefault).Background(ui.Transparent).Border(1, ui.Transparent).
		TextColor(p.Off).AnimatePose(ui.Pose{}, springLift).Tooltip(off)
}

// doneStep stands where a step's button was once the step is done: dimmed and ticked, it
// keeps its place in the bar, so the session still reads from left to right.
func doneStep(c *ui.Context, p Palette, label string) ui.Element {
	tick := p.Success
	return ui.Row(c).Padding(7, 14).Radius(8).Gap(6).Justify(ui.Center).AlignItems(ui.Center).
		Border(1, ui.Transparent).Label(label).Children(func() {
		tickMark(c, tick)
		ui.Text(c, label).FontSize(13).FontWeight(600).TextColor(p.Text3).NoWrap()
	})
}

// busyButton stands where a button will be while its job runs: nothing to click, and a
// band of light travelling along its foot until the job is done.
func busyButton(c *ui.Context, p Palette, label string) ui.Element {
	band := p.Warning
	return ui.Row(c).Padding(7, 16).Radius(8).Justify(ui.Center).AlignItems(ui.Center).Clip().
		Background(p.Raised).Border(1, p.Raised).Label(label).Children(func() {
		ui.Text(c, label).FontSize(13).FontWeight(600).FontFeatures("tnum").TextColor(p.Text2).NoWrap().ContentTransition(numeric)
		ui.Box(c).Absolute().Left(0).Right(0).Bottom(0).Height(2).IgnoreInput(true).
			Draw(func(pt *ui.Painter, r ui.Rect) { sweep(pt, r, band, 0) })
	})
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
				ui.Text(c, title).Font(fontSerif).FontSize(22).Balance()
				if sub != "" {
					ui.Text(c, sub).FontSize(13).TextColor(p.Text3).Pretty()
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
