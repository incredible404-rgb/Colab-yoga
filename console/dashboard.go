package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
	"github.com/egoist/mygo/ui/chart"
)

func (a *App) dashboard(c *ui.Context, p Palette) {
	pageHeader(c, p, "仪表盘", a.dashboardSubtitle())
	// Two cards side by side, wrapping under each other when the window is narrow.
	ui.Row(c).Wrap().Gap(20).AlignItems(ui.Stretch).Children(func() {
		a.billCard(c, p).Basis(420).Grow(1).Shrink(1).MinWidth(340)
		a.modelCard(c, p).Basis(420).Grow(1).Shrink(1).MinWidth(340)
	})
	a.hardwareCard(c, p)
	a.accessCard(c, p)
}

func (a *App) dashboardSubtitle() string {
	st := a.status
	if a.connected && st != nil && st.Uptime > 0 {
		now := a.now()
		up := a.uptimeAt(now)
		since := now.Add(-time.Duration(up * float64(time.Second))).Format("15:04")
		return fmt.Sprintf("这台 GPU %s 分配给你，已运行 %s。对话在网页、Magpie 或 MCP 里进行，这里管算力和模型。", since, hms(up))
	}
	return "对话在网页、Magpie 或 MCP 里进行，这里管算力和模型。"
}

// ─── Bill ─────────────────────────────────────────────────────────────────────

// ledgerLive: the ledger shows a billing session and no newer account reading says nothing is
// assigned any more (a release reaches the account poll before the keeper notices it).
func (a *App) ledgerLive(now time.Time) bool {
	return a.ledger.Live(now) && !(a.acct.Known && a.acct.Active == 0 && a.acctAt.After(a.ledger.CheckedAt))
}

// officialRate is Colab's advertised CU/h for what is assigned now.
func (a *App) officialRate() float64 {
	if a.acct.Burn > 0 {
		return a.acct.Burn
	}
	if a.ledgerLive(a.now()) && a.ledger.OfficialRate > 0 {
		return a.ledger.OfficialRate
	}
	if a.status != nil && a.connected {
		return a.status.Burn
	}
	return 0
}

// burnRate is what the ledger shows being charged once it has enough settlements, else the
// advertised rate; measured says which, steps how many settlements the measurement spans.
func (a *App) burnRate(now time.Time) (rate float64, measured bool, steps int) {
	if a.ledgerLive(now) {
		if r, n, ok := a.ledger.MeasuredRate(); ok {
			return r, true, n
		}
	}
	return a.officialRate(), false, 0
}

// balance is the newer of the account poll and the keeper's ledger reading.
func (a *App) balance() float64 {
	if !a.ledger.CheckedAt.IsZero() && a.ledger.CheckedAt.After(a.acctAt) {
		return a.ledger.Balance
	}
	return a.acct.Balance
}

// sinceStatus is how long ago the status arrived, capped so a stalled poll does not run the
// countdown and the uptime on indefinitely.
func (a *App) sinceStatus(now time.Time) float64 {
	if a.statusAt.IsZero() {
		return 0
	}
	return min(max(0, now.Sub(a.statusAt).Seconds()), 60)
}

func (a *App) idleRemainingAt(now time.Time) float64 {
	return max(0, a.status.IdleRemaining-a.sinceStatus(now))
}

func (a *App) uptimeAt(now time.Time) float64 {
	return a.status.Uptime + a.sinceStatus(now)
}

func agoText(d time.Duration) string {
	if d < time.Minute {
		return "不到 1 分钟前"
	}
	return fmt.Sprintf("%d 分钟前", int(d.Minutes()))
}

func (a *App) billCard(c *ui.Context, p Palette) ui.Element {
	st := a.status
	now := a.now()
	burn, measured, steps := a.burnRate(now)
	official := a.officialRate()
	led := a.ledger
	live := a.ledgerLive(now)
	billing := a.connected || a.acct.Active > 0
	return card(c, p, 1, "算力账单", "余额与扣费以 Colab 官方账本的实际扣减为准", func() {
		if billing && burn > 0 {
			pill(c, "计费中", p.Accent)
		} else {
			pill(c, "未计费", p.Muted)
		}
	}, func() {
		bal := "—"
		var sub []string
		if a.acct.Known && a.acct.LoggedIn {
			bal = thousands(a.balance()) + " CU"
			if burn > 0 {
				sub = append(sub, fmt.Sprintf("按当前速率约可用 %.0f 小时", a.balance()/burn))
			}
			if live {
				if last := led.LastSettle(); !last.IsZero() {
					sub = append(sub, "上次结算 "+agoText(now.Sub(last)))
				} else {
					sub = append(sub, "官方账本约每 5 分钟结算一次")
				}
			}
		}
		metricRoll(c, p, "账户余额", bal, strings.Join(sub, " · "), 38, p.Text, numericDown).
			Tooltip("Colab 官方账本余额。账本约每 5 分钟结算一次，结算之间余额不动")
		ui.Row(c).Wrap().GapX(36).GapY(14).AlignItems(ui.Start).Children(func() {
			rateTip := "Colab 官方报价。账本结算满 3 次（开机约 20 分钟）后改用余额实际扣减算出的速率"
			rateSub := ""
			if measured {
				rateTip = fmt.Sprintf("按官方账本最近 %d 次结算的实际扣减算出；Colab 官方报价 %.2f CU/h", steps, official)
				rateSub = "账本实测"
			} else if burn > 0 {
				rateSub = "官方报价"
			}
			metricRoll(c, p, "扣费速率", fmt.Sprintf("%.2f CU/h", burn), rateSub, 20, map[bool]ui.Color{true: p.Accent, false: p.Text}[burn > 0], numeric).Tooltip(rateTip)
			if st != nil && a.connected {
				metricRoll(c, p, "本次运行", hms(a.uptimeAt(now)), "", 20, p.Text, numeric).Tooltip("从这台 GPU 分配给你（开始计费）算起")
			} else {
				metric(c, p, "本次运行", "—", "", 20, p.Text3)
			}
			switch {
			case live && led.Session.Exact:
				metricRoll(c, p, "本次已花", fmt.Sprintf("%.2f CU", led.Spent()), "账本实扣", 20, p.Text, numeric).
					Tooltip(fmt.Sprintf("开机前余额 %s − 当前余额。账本约每 5 分钟结算，最近几分钟的还没扣", thousands(led.Session.StartBalance)))
			case live:
				metricRoll(c, p, "本次已花", fmt.Sprintf("≥ %.2f CU", led.Spent()), "自 "+led.Session.StartAt.Format("15:04")+" 起实扣", 20, p.Text, numeric).
					Tooltip("没记录到开机前的余额，只算从这之后官方账本的实际扣减")
			case st != nil && a.connected:
				metricRoll(c, p, "本次已花", fmt.Sprintf("≈ %.1f CU", a.uptimeAt(now)/3600*official), "估算", 20, p.Text, numeric).
					Tooltip("运行时长 × 官方报价。后台保活记下账本后改为实扣")
			default:
				metric(c, p, "本次已花", "—", "", 20, p.Text3)
			}
		})
		a.idleBlock(c, p)
		ui.Row(c).Gap(8).Children(func() {
			col, text := p.Muted, "没有实例时不需要保活"
			if a.keeper.Alive {
				col, text = p.Success, "后台保活运行中：关掉这个窗口也不影响 GPU"
			} else if a.connected {
				col, text = p.Warning, "后台保活没在跑，几十秒内会自动启动"
			}
			dot(c, col, 7)
			ui.Text(c, text).FontSize(13).TextColor(p.Text2).ContentTransition(crossfade)
		})
	})
}

func (a *App) idleBlock(c *ui.Context, p Palette) {
	st := a.status
	ui.Column(c).Gap(8).Padding(14).Radius(12).Background(p.Raised).Children(func() {
		ui.Row(c).Gap(10).Children(func() {
			var text string
			switch {
			case st == nil || !a.connected:
				text = fmt.Sprintf("连上后闲置 %d 分钟自动释放", a.idleMin)
				if a.idleMin <= 0 {
					text = "自动释放已关闭，用完记得手动释放"
				}
			case st.IdleTimeoutMin <= 0:
				text = "自动释放已关闭，用完记得手动释放"
			default:
				rem := int(a.idleRemainingAt(a.now()))
				text = fmt.Sprintf("闲置 %d 分 %02d 秒后自动释放（网页 / Magpie / MCP 的对话都算在用）", rem/60, rem%60)
			}
			ui.Text(c, text).FontSize(13).TextColor(p.Text2).Grow(1).Shrink(1).ContentTransition(numericDown)
			outlineButton(c.Key("touch-bill"), p, "续时").Padding(4, 12).Disabled(!a.connected).OnClick(a.touch)
		})
		frac := 0.0
		if st != nil && a.connected && st.IdleTimeoutMin > 0 {
			frac = a.idleRemainingAt(a.now()) / (st.IdleTimeoutMin * 60)
		}
		col := p.Accent
		if frac < 0.2 {
			col = p.Danger
		}
		gauge(c, p, "idle", frac, col, "距离自动释放")
	})
}

// ─── Current model ────────────────────────────────────────────────────────────

func (a *App) modelCard(c *ui.Context, p Palette) ui.Element {
	id, alias, online := a.servedModel()
	st := a.status
	loading := a.switching != "" || (st != nil && a.connected && (st.IsLoading || st.IsDownloading))
	busy := 0
	if st != nil && st.LLM != nil && a.connected {
		busy = st.LLM.SlotsBusy
	} else {
		busy = a.access.Llama.SlotsBusy
	}
	return card(c, p, 2, "当前模型", "云端 :8081 正在为网页、Magpie 和 MCP 服务的模型", func() {
		switch {
		case loading:
			pill(c, "加载中", p.Warning)
		case online && busy > 0:
			pill(c, "推理中", p.Info)
		case online:
			pill(c, "运行中", p.Success)
		case a.connected:
			pill(c, "未加载", p.Muted)
		default:
			pill(c, "未连接", p.Muted)
		}
	}, func() {
		m := a.modelByID(id)
		// One headline that morphs letter by letter as the model changes (Magic Move).
		head, size, col := "云端还没有模型在跑", float32(22), p.Text2
		switch {
		case online && m != nil:
			head, size, col = m.Name, 26, p.Text
		case online:
			head, col = alias, p.Text
		case loading:
			head, col = "正在装载 "+a.modelName(a.switching), p.Text
		}
		h := ui.Text(c.Key("headline"), head).Font(fontSerif).FontSize(size).SingleLine().ContentTransition(morph)
		h.TextColor(h.AnimateColor("c", col, fadeColor))
		if alias != "" {
			ui.Text(c.Key("alias"), alias).Font(fontMono).FontSize(12).TextColor(p.Text3).SingleLine().
				ContentTransition(morph).Transition(rowMotion)
		}
		if m != nil {
			ui.Row(c.Key("chips-"+m.ID)).Wrap().Gap(6).Transition(rowMotion).Children(func() {
				for _, tag := range m.Tags {
					chip(c, p, tag)
				}
				if m.SizeGB > 0 {
					chip(c, p, fmt.Sprintf("%.1f GB", m.SizeGB))
				}
				if n := a.ctxTokens(); n > 0 {
					chip(c, p, fmt.Sprintf("%dK 上下文", n/1024))
				}
			})
		}
		if loading && st != nil {
			ui.Column(c.Key("load")).Gap(6).Transition(rowMotion).Children(func() {
				prog := clamp01(st.TaskProgress)
				if prog <= 0 {
					prog = -1 // nothing to measure yet: sweep instead of sitting at zero
				}
				gauge(c, p, "load", prog, p.Accent, "装载进度")
				ui.Text(c, st.TaskMessage).FontSize(12).TextColor(p.Text3).SingleLine().ContentTransition(numeric)
			})
		}
		if st != nil && a.connected && st.VRAMTotal > 0 {
			ui.Column(c.Key("vram")).Gap(6).Transition(rowMotion).Children(func() {
				ui.Row(c).Children(func() {
					ui.Text(c, "显存").FontSize(12).TextColor(p.Text3).Grow(1)
					ui.Text(c, fmt.Sprintf("%.1f / %.1f GB", st.VRAMUsed, st.VRAMTotal)).Font(fontMono).FontFeatures("tnum").FontSize(12).
						ContentTransition(numeric)
				})
				gauge(c, p, "vram", st.VRAMUsed/st.VRAMTotal, p.ChartRAM, "显存占用")
			})
		}
		ui.Row(c).Wrap().GapX(32).GapY(12).Children(func() {
			slots, last := "—", "—"
			if st != nil && st.LLM != nil && st.LLM.Online && a.connected {
				slots = fmt.Sprintf("%d / %d 忙", st.LLM.SlotsBusy, st.LLM.SlotsTotal)
				last = ago(st.LLM.LastActiveAge)
			} else if a.access.Llama.OK {
				slots = fmt.Sprintf("%d / %d 忙", a.access.Llama.SlotsBusy, a.access.Llama.SlotsTotal)
			}
			metricRoll(c, p, "推理槽位", slots, "", 16, p.Text, numeric)
			metric(c, p, "最近一次推理", last, "", 16, p.Text)
			mcpText, mcpCol := a.mcpSummary(p)
			metric(c, p, "小说 MCP", mcpText, "", 16, mcpCol)
		})
		ui.Row(c).Gap(8).Wrap().Children(func() {
			label := "切换模型"
			if !online {
				label = "加载模型"
			}
			ui.Button(c, label+" ▾").Padding(7, 16).Radius(8).Background(p.Ink).TextColor(p.InkText).
				FontSize(13).FontWeight(600).Disabled(!a.connected || loading || len(a.catalog) == 0).Menu(func(menu *ui.Menu) {
				for i := range a.catalog {
					mm := &a.catalog[i]
					item := menu.Item(modelLabel(mm)).Checked(online && mm.ID == id)
					if item.Chosen() && mm.ID != id {
						a.switchTarget, a.askSwitch = mm.ID, true
					}
				}
			})
			outlineButton(c.Key("web-model"), p, "打开网页对话").Disabled(!online).OnClick(a.openWeb)
			dangerButton(c.Key("unload"), p, "卸载").Disabled(!online || !a.connected || loading).
				OnClick(func() { a.askUnload = true })
		})
	})
}

func (a *App) ctxTokens() int {
	if st := a.status; st != nil && st.LLM != nil && st.LLM.NCtx > 0 && a.connected {
		return st.LLM.NCtx
	}
	return a.access.Llama.NCtx
}

func (a *App) mcpSummary(p Palette) (string, ui.Color) {
	if !a.access.MCPUp && !a.access.At.IsZero() {
		return "本机服务没开", p.Danger
	}
	if st := a.status; st != nil && st.LLM != nil && st.LLM.MCP != nil && a.connected {
		b := st.LLM.MCP
		switch {
		case !b.Listening:
			return "云端转发没起", p.Danger
		case b.Reachable != nil && !*b.Reachable:
			return "云端连不到本机", p.Danger
		case b.Reachable != nil && *b.Reachable:
			return "已连通", p.Success
		}
		return "检查中", p.Text3
	}
	return "—", p.Text3
}

func chip(c *ui.Context, p Palette, text string) {
	ui.Box(c.Key(text)).Padding(3, 10).Radius(999).Border(1, p.BorderStrong).Children(func() {
		ui.Text(c, text).FontSize(12).TextColor(p.Text2).NoWrap()
	})
}

// ─── Hardware ─────────────────────────────────────────────────────────────────

func (a *App) hardwareCard(c *ui.Context, p Palette) {
	card(c, p, 3, "硬件", "近 2 分钟走势，每 2.5 秒一个点", nil, func() {
		ui.Row(c).Wrap().Gap(14).AlignItems(ui.Stretch).Children(func() {
			st := a.status
			on := st != nil && a.connected
			val := func(used, total float64) string {
				if !on || total <= 0 {
					return "—"
				}
				return fmt.Sprintf("%.1f / %.0f GB", used, total)
			}
			var vu, vt, ru, rt, du, dt float64
			if on {
				vu, vt, ru, rt, du, dt = st.VRAMUsed, st.VRAMTotal, st.RAMUsed, st.RAMTotal, st.DiskUsed, st.DiskTotal
			}
			a.sparkTile(c, p, "GPU 显存", val(vu, vt), a.hist[0], p.ChartVRAM)
			a.sparkTile(c, p, "系统内存", val(ru, rt), a.hist[1], p.ChartRAM)
			a.sparkTile(c, p, "实例磁盘", val(du, dt), a.hist[2], p.ChartDisk)
		})
	})
}

func (a *App) sparkTile(c *ui.Context, p Palette, label, value string, hist []float64, col ui.Color) {
	tile := ui.Column(c.Key(label)).Basis(240).Grow(1).Shrink(1).MinWidth(200).Padding(14, 16).Gap(10).Radius(12).
		Background(p.Raised)
	hoverLift(tile, p)
	tile.Children(func() {
		ui.Row(c).Children(func() {
			ui.Text(c, label).FontSize(13).TextColor(p.Text2).Grow(1)
			ui.Text(c, value).Font(fontMono).FontFeatures("tnum").FontSize(12).NoWrap().ContentTransition(numeric)
		})
		samples := append([]float64(nil), hist...)
		at := a.histAt
		ui.Box(c).Height(64).FillWidth().Draw(func(pt *ui.Painter, r ui.Rect) {
			pt.Line(r.X, r.Y+r.H, r.X+r.W, r.Y+r.H, 1, p.BorderStrong)
			if len(samples) < 2 {
				return
			}
			// Each new sample slides in from the right edge rather than the line jumping
			// a step every 2.5 s, as a live chart scrolls; then a ring ripples from the
			// newest point. Computed from the clock, repainted without running the view,
			// and only for the first 900 ms after a sample: still in between.
			k := float32(1)
			if dt := pt.Now().Sub(at); dt >= 0 && dt < sparkSlide {
				k = float32(dt) / float32(sparkSlide)
				pt.AnimationFrame()
			}
			step := float32(1) / float32(histLen-1)
			slide := (1 - easeStandard(min(1, k*1.3))) * step
			xs := make([]float32, len(samples))
			ys := make([]float32, len(samples))
			for i, v := range samples {
				xs[i] = 1 - float32(len(samples)-1-i)*step + slide
				ys[i] = float32(clamp01(v))
			}
			pt.Clip(r, 0, func() {
				pt.FillPathGradient(chart.Area(r, xs, nil, ys, chart.CurveMonotone), ui.LinearGradient{
					From: col.Alpha(0.26), To: col.Alpha(0.02), Angle: 180,
				})
				pt.StrokePath(chart.Line(r, xs, ys, chart.CurveMonotone), 2, col)
			})
			last := len(samples) - 1
			hx, hy := r.X+r.W*xs[last], r.Y+r.H*(1-ys[last])
			if hx > r.X+r.W+1 {
				return
			}
			if k < 1 && !pt.Preferences().ReduceMotion {
				e := ui.EaseOut(k)
				rr := 3 + 9*e
				pt.Fill(ui.Rect{X: hx - rr, Y: hy - rr, W: 2 * rr, H: 2 * rr}, col.Alpha(0.35*(1-e)), rr)
			}
			pt.Fill(ui.Rect{X: hx - 3, Y: hy - 3, W: 6, H: 6}, col, 3)
		})
	})
}

// sparkSlide is how long a new sample takes to slide in and ripple.
const sparkSlide = 900 * time.Millisecond

// ─── Ways in ──────────────────────────────────────────────────────────────────

type wayIn struct {
	title, value, note string
	state             string // ok | warn | down | idle
	actions           []action
}

type action struct {
	label   string
	primary bool
	run     func()
}

func (a *App) accessCard(c *ui.Context, p Palette) {
	card(c, p, 4, "接入", "对话在这些入口里进行；绿灯 = 从这台电脑实测可用", func() {
		ghostButton(c.Key("edit-urls"), p, "修改地址").OnClick(func() { a.page = "connection" })
	}, func() {
		ui.Row(c).Wrap().Gap(12).AlignItems(ui.Stretch).Children(func() {
			for _, w := range a.waysIn() {
				a.wayRow(c, p, w)
			}
		})
	})
}

func (a *App) waysIn() []wayIn {
	acc := a.access
	ts := a.tsURL
	api := ""
	if ts != "" {
		api = ts + "/v1"
	}
	checking := acc.At.IsZero()
	down := fmt.Sprintf("不通（%s）：云端没有模型在跑，或这台电脑没连上 Tailscale。", acc.Llama.Err)
	_, alias, _ := a.servedModel()

	web := wayIn{title: "网页对话", value: ts, actions: []action{{"打开", true, a.openWeb}, {"复制", false, func() { a.copy(ts, "已复制网页地址") }}}}
	apiW := wayIn{title: "OpenAI 兼容 API", value: api, actions: []action{
		{"复制地址", false, func() { a.copy(api, "已复制 API 地址") }},
		{"复制模型名", false, func() { a.copy(alias, "已复制模型名") }},
	}}
	switch {
	case ts == "":
		web.state, web.note = "idle", "没配置 Tailscale 地址，去「连接与账号」填写。"
		apiW.state, apiW.note = web.state, web.note
	case checking:
		web.state, web.note = "idle", "检测中…"
		apiW.state, apiW.note = "idle", "检测中…"
	case acc.Llama.OK:
		web.state, web.note = "ok", "浏览器直接打开 llama-server 网页界面；只在 tailnet 内可见，免密钥。首次打开要下载约 3 MB 页面资源，之后走缓存。"
		apiW.state, apiW.note = "ok", "模型名 "+alias+"；tailnet 内无需 API Key。"
	default:
		web.state, web.note = "down", down
		apiW.state, apiW.note = "down", down
	}

	mg := acc.Magpie
	model := ""
	if mg.ProviderID != "" && alias != "" {
		model = mg.ProviderID + "/" + alias
	}
	mag := wayIn{title: "Magpie", value: model, actions: []action{{"复制模型名", false, func() { a.copy(model, "已复制 Magpie 模型名") }}}}
	switch {
	case checking:
		mag.state, mag.note = "idle", "检测中…"
	case mg.ProviderID == "":
		mag.state, mag.note = "warn", "Magpie 里没有指向这个 Tailscale 地址的供应商。"
	case mg.Off:
		mag.state, mag.note = "warn", fmt.Sprintf("「%s」供应商在 Magpie 里是关闭的，各 agent 选不到 %s/*；在 Magpie 里打开即可。", mg.ProviderName, mg.ProviderID)
	case mg.GatewayUp == nil:
		mag.state, mag.note = "idle", "没找到 magpie 命令，无法确认网关。"
	case !*mg.GatewayUp:
		mag.state, mag.note = "down", "Magpie 网关没在运行。"
	case !acc.Llama.OK:
		mag.state, mag.note = "down", "供应商已开，但云端没有模型在跑。"
	case !mg.Listed:
		mag.state, mag.note = "warn", "当前模型不在该供应商的模型列表里（colab_kernel.py --sync-magpie 会补上）。"
	default:
		mag.state, mag.note = "ok", "各 agent 选 "+model+" 即可。"
	}

	mcpText, _ := a.mcpSummary(darkPalette)
	mcp := wayIn{title: "MCP 小说桥", value: a.novelRoot, actions: []action{{"打开文件夹", false, func() { a.openFolder(a.novelRoot) }}}}
	switch {
	case checking:
		mcp.state, mcp.note = "idle", "检测中…"
	case !acc.MCPUp:
		mcp.state, mcp.note = "down", "本机 MCP 服务（local_novel_mcp.py）没在运行，网页里的小说目录工具会报错。"
	case mcpText == "已连通":
		mcp.state, mcp.note = "ok", "云端模型可以读写这个工作文件夹；在网页输入框的工具开关里打开 local_novel_fs。"
	case mcpText == "云端转发没起" || mcpText == "云端连不到本机":
		mcp.state, mcp.note = "down", mcpText+"：服务端会在 15 秒内自动补上转发；仍不通就检查本机 Tailscale。"
	default:
		mcp.state, mcp.note = "ok", "本机服务在监听；云端模型跑起来后会核对云端到本机的隧道。"
	}
	return []wayIn{web, mag, apiW, mcp}
}

func (a *App) wayRow(c *ui.Context, p Palette, w wayIn) {
	col := map[string]ui.Color{"ok": p.Success, "warn": p.Warning, "down": p.Danger, "idle": p.Muted}[w.state]
	row := ui.Column(c.Key(w.title)).Basis(440).Grow(1).Shrink(1).MinWidth(320).Padding(14, 16).Gap(8).Radius(12).
		Background(p.Raised)
	hoverLift(row, p)
	row.Children(func() {
		ui.Row(c).Gap(10).Children(func() {
			dot(c, col, 8)
			ui.Text(c, w.title).FontSize(14).FontWeight(650).NoWrap()
			ui.Text(c, w.value).Font(fontMono).FontSize(12).TextColor(p.Text3).SingleLine().
				Grow(1).Shrink(1).MinWidth(40).Tooltip(w.value)
			for _, act := range w.actions {
				act := act
				var b ui.Element
				if act.primary {
					b = inkButton(c.Key(w.title+act.label), p, act.label)
				} else {
					b = outlineButton(c.Key(w.title+act.label), p, act.label)
				}
				b.Padding(4, 12).FontSize(12).OnClick(act.run)
			}
		})
		ui.Text(c, w.note).FontSize(12).TextColor(p.Text3).ContentTransition(crossfade)
	})
}

func (a *App) copy(text, msg string) {
	if strings.TrimSpace(text) == "" {
		return
	}
	a.clip = text
	if a.live {
		mygo.Clipboard.WriteText(text)
	}
	a.flash(msg)
}

func clamp01(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}
