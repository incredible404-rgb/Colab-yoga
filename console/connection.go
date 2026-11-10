package main

import (
	"fmt"
	"strings"

	"github.com/egoist/mygo/ui"
)

var gpuChoices = []string{"G4", "H100", "A100", "L4", "T4"}
var idleChoices = []int{15, 30, 60, 0}

func (a *App) connectionPage(c *ui.Context, p Palette) {
	pageHeader(c, p, "连接与账号", "绑定 Google 账号，唤醒、找回和释放 Colab 实例。")
	ui.Row(c).Wrap().Gap(20).AlignItems(ui.Stretch).Children(func() {
		a.accountCard(c, p).Basis(420).Grow(1).Shrink(1).MinWidth(340)
		a.runtimeCard(c, p).Basis(420).Grow(1).Shrink(1).MinWidth(340)
	})
	a.serverCard(c, p)
	a.idleCard(c, p)
	a.urlsCard(c, p)
}

func (a *App) accountCard(c *ui.Context, p Palette) ui.Element {
	return card(c, p, 1, "Google 账号", "用来申请 Colab GPU 和查官方账本", nil, func() {
		switch {
		case !a.acct.Known:
			ui.Text(c, "查询中…").TextColor(p.Text3)
		case a.acct.LoggedIn:
			ui.Row(c).Gap(10).Children(func() {
				dot(c, p.Success, 8)
				ui.Text(c, maskEmail(a.acct.Email)).FontSize(15).SingleLine()
			})
			metric(c, p, "账户余额", thousands(a.acct.Balance)+" CU", "", 22, p.Text)
		default:
			ui.Row(c).Gap(10).Children(func() {
				dot(c, p.Muted, 8)
				ui.Text(c, "未绑定").FontSize(15)
			})
		}
		if a.binding {
			ui.Column(c.Key("binding")).Gap(8).Padding(14).Radius(12).Background(p.Raised).Transition(rowMotion).Children(func() {
				ui.Text(c, "在浏览器里完成授权，把页面上显示的授权码粘贴到这里。").FontSize(13).TextColor(p.Text2)
				ui.Row(c).Gap(8).Children(func() {
					in := ui.TextInput(c.Key("auth-code"), &a.authCode).Placeholder("授权码").Password().
						Font(fontMono).FontSize(13).Grow(1).Shrink(1).MinWidth(120).Label("授权码")
					if in.Submitted() {
						a.submitAuthCode()
					}
					inkButton(c.Key("auth-submit"), p, "确认").Disabled(a.authBusy || strings.TrimSpace(a.authCode) == "").
						OnClick(a.submitAuthCode)
					ghostButton(c.Key("auth-cancel"), p, "取消").OnClick(func() { a.binding, a.authCode = false, "" })
				})
			})
		}
		ui.Row(c).Gap(8).Wrap().Children(func() {
			label := "绑定 Google 账号"
			if a.acct.LoggedIn {
				label = "换号"
			}
			if a.authBusy {
				label = "处理中…"
			}
			outlineButton(c.Key("bind"), p, label).Disabled(a.authBusy || a.binding).OnClick(a.startBinding)
			if a.acct.LoggedIn {
				ghostButton(c.Key("logout"), p, "退出账号").Disabled(a.authBusy).OnClick(func() { a.askLogout = true })
			}
			ghostButton(c.Key("acct-refresh"), p, "刷新").OnClick(func() { a.once("acct", a.refreshAccount) })
		})
	})
}

func (a *App) runtimeCard(c *ui.Context, p Palette) ui.Element {
	return card(c, p, 2, "云端实例", "机房由 Google 按空闲分配，不能指定", nil, func() {
		code, region := regionOf(a.acct.Endpoint)
		ui.Row(c).Wrap().GapX(32).GapY(12).Children(func() {
			metric(c, p, "运行中的实例", fmt.Sprintf("%d 台", a.acct.Active), "", 20, p.Text)
			loc := "—"
			if code != "" {
				loc = strings.TrimSpace(region + " " + code)
			}
			metric(c, p, "机房", loc, "", 20, p.Text)
			acc := a.acct.Accel
			if acc == "" {
				acc = "—"
			}
			metric(c, p, "GPU", acc, "", 20, p.Text)
		})
		ui.Text(c, "唤醒时申请的 GPU").FontSize(12).TextColor(p.Text3)
		idx := 0
		for i, g := range gpuChoices {
			if g == a.gpu {
				idx = i
			}
		}
		if ui.Segmented(c, &idx, gpuChoices...).Label("GPU 型号").Changed() {
			a.gpu = gpuChoices[idx]
			_ = a.set.Set(map[string]any{"wakeGPUChoiceV2": a.gpu})
		}
		ui.Row(c).Gap(8).Wrap().Children(func() {
			label := "唤醒实例并部署"
			if a.waking {
				label = "唤醒部署中…"
			} else if a.acct.Active > 0 {
				label = "重新部署服务端"
			}
			inkButton(c.Key("wake"), p, label).Disabled(a.waking || !a.acct.LoggedIn).
				Tooltip("已有实例时复用同一台机器，只重启服务端；:8081 上的模型不受影响").OnClick(a.wake)
			dangerButton(c.Key("release-all"), p, "释放全部实例").Disabled(a.releasing || a.acct.Active == 0).
				OnClick(func() { a.askRelease = true })
		})
	})
}

func (a *App) serverCard(c *ui.Context, p Palette) {
	card(c, p, 3, "服务地址", "云端服务端的公网地址；丢了会自动从云端找回", nil, func() {
		ui.Row(c).Gap(8).Children(func() {
			in := ui.TextInput(c.Key("url"), &a.urlDraft).Placeholder("https://xxxx.gradio.live（留空点「找回」）").
				Font(fontMono).FontSize(13).Grow(1).Shrink(1).MinWidth(120).Label("服务地址")
			if in.Submitted() {
				a.setServerURL(a.urlDraft, true)
			}
			outlineButton(c.Key("connect"), p, "连接").OnClick(func() {
				if strings.TrimSpace(a.urlDraft) == "" {
					a.discover(true)
				} else {
					a.setServerURL(a.urlDraft, true)
				}
			})
			outlineButton(c.Key("discover"), p, "从云端找回").Disabled(a.discovering).OnClick(func() { a.discover(true) })
		})
		var col ui.Color
		var text string
		switch {
		case a.connected:
			col, text = p.Success, "已连接"
		case a.discovering:
			col, text = p.Warning, "正在从云端找回…"
		case a.serverURL == "":
			col, text = p.Muted, "没有地址"
		default:
			col, text = p.Danger, fmt.Sprintf("连不上（已失败 %d 次）", a.failures)
		}
		ui.Row(c).Gap(8).Children(func() {
			statusDot(c, col, 7, a.discovering)
			ui.Text(c, text).FontSize(13).TextColor(p.Text2).ContentTransition(crossfade)
		})
	})
}

func (a *App) idleCard(c *ui.Context, p Palette) {
	card(c, p, 4, "自动释放", "闲置这么久没人用就释放实例，网页 / Magpie / MCP 的对话都算在用", nil, func() {
		labels := []string{"15 分钟", "30 分钟", "60 分钟", "关闭"}
		idx := 1
		for i, m := range idleChoices {
			if m == a.idleMin {
				idx = i
			}
		}
		if ui.Segmented(c, &idx, labels...).Label("自动释放").Changed() {
			a.setIdle(idleChoices[idx])
		}
		if a.idleMin == 0 {
			ui.Text(c.Key("idle-off"), "关闭后会一直计费直到你手动释放（后台保活最多撑 24 小时）。").FontSize(13).TextColor(p.Warning).
				Transition(rowMotion)
		}
	})
}

func (a *App) urlsCard(c *ui.Context, p Palette) {
	card(c, p, 5, "接入地址", "网页对话与 API 走 Tailscale Service；MCP 小说桥在本机", nil, func() {
		field := func(label, key, hint string, v *string) {
			ui.Column(c).Gap(6).Children(func() {
				ui.Text(c, label).FontSize(12).TextColor(p.Text3)
				ui.TextInput(c.Key(key), v).Placeholder(hint).Font(fontMono).FontSize(13).Label(label)
			})
		}
		field("Tailscale Service 地址", "ts", "http://qwen.<tailnet>.ts.net:443", &a.tsURL)
		field("本机 MCP 小说桥", "mcp", "http://127.0.0.1:3426", &a.mcpURL)
		field("工作文件夹", "root", `C:\Users\…\Documents\Colab创作`, &a.novelRoot)
		ui.Row(c).Gap(8).Children(func() {
			inkButton(c.Key("save-urls"), p, "保存").OnClick(func() {
				a.saveAccessURLs()
				a.flash("已保存接入地址")
			})
			outlineButton(c.Key("open-root"), p, "打开工作文件夹").OnClick(func() { a.openFolder(a.novelRoot) })
		})
	})
}

func maskEmail(e string) string {
	user, domain, ok := strings.Cut(e, "@")
	if !ok || len(user) < 4 {
		return e
	}
	return user[:3] + "…@" + domain
}
