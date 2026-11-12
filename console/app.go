package main

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/egoist/mygo"
)

// Account is what colab_bridge auth_status reports (official Colab ledger).
type Account struct {
	Known    bool
	LoggedIn bool
	Email    string
	Balance  float64
	Burn     float64
	Active   int
	Endpoint string
	Accel    string
}

const histLen = 48

// App holds the console's state. The view reads it on the main thread; background
// work changes it only through apply. The window comes and goes (closing it destroys it
// to give its memory back; the tray brings it back) while App and its loop keep running.
type App struct {
	live   bool         // running under mygo (false in tests)
	win    *mygo.Window // nil while only the tray icon shows
	tray   *mygo.Tray
	paths  Paths
	set    *Settings
	server *Server
	bridge *Bridge

	// persisted (settings.json)
	serverURL, tsURL, mcpURL, novelRoot string
	catalog                             []Model // from colab_kernel_runners.json
	catalogSig                          runnersSig
	gpu                                 string
	idleMin                             int
	themeName                           string // "dark" | "light"
	ctxK                                int
	hfToken                             string

	// live
	page              string // "dashboard" | "connection"
	navWidth          float32
	connected         bool
	failures          int
	status            *Status
	statusAt          time.Time // when status arrived: the countdown and uptime run on from it
	acct              Account
	acctAt            time.Time
	ledger            Ledger
	access            Access
	keeper            Keeper
	hist              [3][]float64
	histAt            time.Time // when the last samples came, for the charts to slide them in
	logs              []string
	serverLog         []string
	logsOpen          bool
	logsAlert         bool
	waking            bool
	mcpStarting       bool      // a manual start of the local MCP is in flight
	mcpStartErr       string    // why the last manual start failed; cleared by the next start or a success
	wakeAt            time.Time // when the wake in flight began
	redeploying       bool      // the wake in flight reuses a runtime that was already assigned
	wakeStage         int       // index into wakeStages
	wakeLine          string    // the bridge's latest line, as the card shows it
	wakeErr           string    // why the last wake failed: shown until the next wake, a connection, or dismissed
	switchAt          time.Time // when this console asked for the model being loaded
	lastModel         string    // the model loaded last time: what the load button offers first
	pickOpen          bool      // the load button's library is open
	pickID            string    // the model chosen in it
	discovering       bool
	switching         string // model id being loaded
	releasing         bool
	userClearedURL    bool
	idleWarned        bool
	lastDiscover      time.Time
	lastKeeperSpawn   time.Time
	urlDraft          string
	askRelease        bool
	askSwitch         bool
	askUnload         bool
	switchTarget      string
	binding           bool   // waiting for the Google authorization code
	skipSignIn        bool   // the sign-in page was passed by for now: the console shows with no account bound
	authCode          string // pasted code; cleared right after use, never logged
	tokenStale        bool   // a control call was refused; the next discover refreshes the token
	authBusy          bool
	askLogout         bool
	toast             string
	toastUndo         func()   // with it, the toast carries an Undo button
	clip              string   // last text copied (tests read it)
	opened            []string // URLs and folders handed to the shell (tests read it)
	toastAt           time.Time
	inflight          sync.Map
	tick              int
	now               func() time.Time
	disableBackground bool // tests
}

func newApp(p Paths) *App {
	a := &App{paths: p, set: &Settings{path: p.Settings}, server: newServer(p.Token), bridge: &Bridge{p: p},
		page: "dashboard", navWidth: 280, now: time.Now}
	m := a.set.Load()
	a.serverURL = str(m, "colabServerURL", "")
	a.tsURL = str(m, "accessTailscaleURL", "")
	a.reloadCatalog() // also fills an empty access address from the runners
	a.mcpURL = str(m, "accessMcpURL", "http://127.0.0.1:3426")
	a.novelRoot = str(m, "accessNovelRoot", "")
	if a.novelRoot == "" {
		a.novelRoot = homeDir() + `\Documents\Colab创作`
	}
	a.gpu = str(m, "wakeGPUChoiceV2", "G4")
	a.idleMin = int(num(m, "idleTimeoutMinutes", 30))
	a.themeName = str(m, "consoleTheme", "dark")
	a.ctxK = int(num(m, "qwenContextLengthK", 256))
	a.hfToken = str(m, "hfToken", "")
	a.lastModel = str(m, "selectedModelID", "")
	a.urlDraft = a.serverURL
	a.keeper = readKeeper(p.Keeper)
	a.ledger = readLedger(p.Ledger)
	return a
}

// apply runs fn on the main thread and redraws the window if one shows (directly in
// tests, which have no main loop).
func (a *App) apply(fn func()) {
	if !a.live {
		fn()
		return
	}
	mygo.RunOnMain(func() {
		fn()
		if a.win != nil {
			a.win.Invalidate()
		}
	})
}

// once runs fn in the background unless a run with the same name is still going.
func (a *App) once(name string, fn func()) {
	if a.disableBackground {
		return
	}
	if _, busy := a.inflight.LoadOrStore(name, true); busy {
		return
	}
	go func() {
		defer a.inflight.Delete(name)
		fn()
	}()
}

func (a *App) logf(format string, args ...any) {
	line := time.Now().Format("15:04:05 ") + fmt.Sprintf(format, args...)
	a.logs = append(a.logs, line)
	if len(a.logs) > 300 {
		a.logs = a.logs[len(a.logs)-300:]
	}
}

func (a *App) flash(msg string) {
	a.toast, a.toastAt = msg, a.now()
	a.logf("%s", msg)
}

// ─── Background loop ──────────────────────────────────────────────────────────

func (a *App) run() {
	a.once("acct", a.refreshAccount)
	a.once("access", a.refreshAccess)
	go a.secondHand()
	t := time.NewTicker(2500 * time.Millisecond)
	for range t.C {
		a.apply(func() { a.tick++ })
		a.step()
	}
}

// step runs every 2.5 s. With the window closed (tray only) it polls the server every
// 30 s, which still feeds the idle-release warning, and stops the access probes.
func (a *App) step() {
	var url string
	var tick int
	var connected, hidden bool
	var active int
	var loggedIn bool
	var ledgerAt time.Time
	done := make(chan struct{})
	a.apply(func() {
		url, tick, connected, active, loggedIn = a.serverURL, a.tick, a.connected, a.acct.Active, a.acct.LoggedIn
		ledgerAt = a.ledger.CheckedAt
		hidden = a.live && a.win == nil
		close(done)
	})
	<-done
	if led := readLedger(a.paths.Ledger); !led.CheckedAt.Equal(ledgerAt) {
		a.apply(func() {
			a.ledger = led
			a.updateTray()
		})
	}
	due := schedule(tick, hidden, connected, active)
	if url != "" && due.status {
		a.once("status", func() { a.pollStatus(url) })
	}
	if due.account {
		a.once("acct", a.refreshAccount)
	}
	if due.access {
		a.once("access", a.refreshAccess)
	}
	if tick%12 == 0 {
		a.once("keeper", a.ensureKeeper)
	}
	if loggedIn && active > 0 {
		a.apply(func() {
			lost := a.serverURL == "" || (!a.connected && a.failures >= 4)
			if lost && !a.userClearedURL && !a.waking && time.Since(a.lastDiscover) > 2*time.Minute {
				a.discover(false)
			}
		})
	}
}

// secondHand redraws a shown window once a second while it has a clock to run: connected,
// the idle countdown and the uptime between polls (applyStatus re-anchors them on every
// poll); waking, the time the wake has taken. Nothing is drawn while only the tray icon shows.
func (a *App) secondHand() {
	t := time.NewTicker(time.Second)
	for range t.C {
		mygo.RunOnMain(func() {
			if a.win != nil && (a.connected && a.status != nil || a.waking) {
				a.win.Invalidate()
			}
		})
	}
}

type due struct{ status, account, access bool }

// schedule says what the 2.5 s tick polls. Shown: status every tick, account every 40
// s while connected, access probes every 10 s while a runtime exists. Tray only: status
// every 30 s (enough for the 5-minute idle warning), account every 80 s, no probes.
func schedule(tick int, hidden, connected bool, active int) due {
	if hidden {
		return due{status: tick%12 == 0, account: tick%32 == 0}
	}
	acctEvery := 32
	if connected {
		acctEvery = 16
	}
	return due{
		status:  true,
		account: tick%acctEvery == 0,
		access:  tick%4 == 0 && (connected || active > 0) || tick%24 == 0,
	}
}

func (a *App) pollStatus(url string) {
	st, err := a.server.Status(url, "")
	a.apply(func() {
		if url != a.serverURL {
			return
		}
		if err != nil {
			a.failures++
			if a.failures >= 5 && a.connected {
				a.connected = false
				a.logf("云端服务连不上了（%s），等待自动找回…", shortErr(err))
			}
			return
		}
		was := a.connected
		a.applyStatus(st)
		if !was {
			a.logf("已连接云端 %s", st.GPUName)
		}
	})
}

func (a *App) applyStatus(st *Status) {
	a.status, a.connected, a.failures = st, true, 0
	a.statusAt = a.now()
	a.wakeErr = ""
	push := func(i int, used, total float64) {
		if total <= 0 {
			return
		}
		a.histAt = a.now()
		a.hist[i] = append(a.hist[i], used/total)
		if len(a.hist[i]) > histLen {
			a.hist[i] = a.hist[i][len(a.hist[i])-histLen:]
		}
	}
	push(0, st.VRAMUsed, st.VRAMTotal)
	push(1, st.RAMUsed, st.RAMTotal)
	push(2, st.DiskUsed, st.DiskTotal)
	if len(st.Console) > 0 {
		a.serverLog = st.Console
	}
	if st.TaskMessage != "" && strings.ContainsAny(st.TaskMessage, "失败异常") {
		a.logsAlert = !a.logsOpen
	}
	if a.switching != "" && !st.IsLoading && !st.IsDownloading {
		id := a.switching
		a.switching = ""
		if st.LLM != nil && st.LLM.Online && st.LLM.ModelID == id {
			a.flash(a.modelName(id) + " 已就绪")
		} else {
			a.notify(a.modelName(id)+" 加载失败", st.TaskMessage)
		}
	}
	a.warnIdle()
}

func (a *App) warnIdle() {
	st := a.status
	if st == nil || st.IdleTimeoutMin <= 0 || st.IdleRemaining < 0 {
		return
	}
	if st.IdleRemaining <= 300 && !a.idleWarned {
		a.idleWarned = true
		mins := int(st.IdleRemaining) / 60
		if mins < 1 {
			mins = 1
		}
		a.notify("Colab 实例快被自动释放", fmt.Sprintf("已闲置，约 %d 分钟后释放。还要用就点「续时」，或在网页里发条消息。", mins))
	} else if st.IdleRemaining > 360 {
		a.idleWarned = false
	}
}

func (a *App) notify(title, body string) {
	a.logf("%s：%s", title, body)
	a.logsAlert = !a.logsOpen
	if !a.live {
		return
	}
	go func() {
		n := mygo.NewNotification(mygo.NotificationOptions{Title: title, Body: body})
		_ = n.Show()
	}()
}

func (a *App) refreshAccount() {
	m, err := a.bridge.RunJSON(75*time.Second, "auth_status")
	a.apply(func() {
		if err != nil {
			a.logf("查询 Colab 账号失败：%s", shortErr(err))
			return
		}
		acct, perr := parseAccount(m)
		if perr != "" {
			a.logf("查询 Colab 账号失败：%s", perr)
			return
		}
		a.acct, a.acctAt = acct, a.now()
		a.updateTray()
	})
}

// updateTray keeps the tray tooltip on the two numbers that matter.
func (a *App) updateTray() {
	if a.tray == nil {
		return
	}
	tip := "Colab-yoga"
	switch {
	case !a.acct.Known:
	case !a.acct.LoggedIn:
		tip += " · 未绑定 Google 账号"
	case a.acct.Active > 0:
		rate, _, _ := a.burnRate(a.now())
		tip += fmt.Sprintf(" · 计费中 %.2f CU/h · 余额 %s CU", rate, thousands(a.balance()))
	default:
		tip += fmt.Sprintf(" · 不在计费 · 余额 %s CU", thousands(a.balance()))
	}
	a.tray.SetToolTip(tip)
}

// ─── Google account (bridge auth_url / auth_code / auth_logout) ───────────────

func (a *App) startBinding() {
	if a.authBusy {
		return
	}
	a.authBusy = true
	a.once("auth_url", func() {
		m, err := a.bridge.RunJSON(60*time.Second, "auth_url")
		a.apply(func() {
			a.authBusy = false
			u, _ := m["auth_url"].(string)
			if err != nil || u == "" {
				msg := str(m, "error", "")
				if err != nil {
					msg = shortErr(err)
				}
				a.flash("生成授权链接失败：" + msg)
				return
			}
			a.binding, a.authCode = true, ""
			a.openURL(u)
			a.flash("已在浏览器打开 Google 授权页，授权后把页面上的授权码粘贴回来")
		})
	})
}

func (a *App) submitAuthCode() {
	code := strings.TrimSpace(a.authCode)
	a.authCode = ""
	if code == "" || a.authBusy {
		return
	}
	a.authBusy = true
	a.once("auth_code", func() {
		m, err := a.bridge.RunJSON(90*time.Second, "auth_code", code)
		a.apply(func() {
			a.authBusy = false
			ok, _ := m["ok"].(bool)
			if err != nil || !ok {
				msg := str(m, "error", "授权码无效或已过期，请重新绑定")
				if err != nil {
					msg = shortErr(err)
				}
				a.flash("绑定失败：" + msg)
				return
			}
			a.binding, a.skipSignIn = false, false
			a.flash("已绑定 Google 账号")
		})
		a.refreshAccount()
	})
}

func (a *App) logout() {
	a.once("auth_logout", func() {
		_, err := a.bridge.RunJSON(60*time.Second, "auth_logout")
		a.apply(func() {
			if err != nil {
				a.flash("退出账号失败：" + shortErr(err))
				return
			}
			a.flash("已退出 Google 账号")
		})
		a.refreshAccount()
	})
}

// parseAccount reads auth_status. ok:false is an API error, not an empty ledger: the
// caller keeps the last good numbers rather than show 0 CU and "not billing", which
// would also disable Release and the keeper while a runtime may be billing.
func parseAccount(m map[string]any) (Account, string) {
	if ok, _ := m["ok"].(bool); !ok {
		return Account{}, str(m, "error", "未知错误")
	}
	acct := Account{Known: true}
	acct.LoggedIn, _ = m["logged_in"].(bool)
	acct.Email, _ = m["email"].(string)
	acct.Balance = num(m, "paid_balance", 0)
	acct.Burn = num(m, "burn_rate_hourly", 0)
	acct.Active = int(num(m, "active_assignments", 0))
	if ss, ok := m["sessions"].([]any); ok && len(ss) > 0 {
		if s0, ok := ss[0].(map[string]any); ok {
			acct.Endpoint, _ = s0["endpoint"].(string)
			acct.Accel, _ = s0["accelerator"].(string)
		}
	}
	return acct, ""
}

func (a *App) refreshAccess() {
	var ts, mcp string
	done := make(chan struct{})
	a.apply(func() { ts, mcp = a.tsURL, a.mcpURL; close(done) })
	<-done
	acc := probeAccess(ts, mcp)
	a.apply(func() {
		a.access = acc
		a.reloadCatalog() // a runners file exported while the console runs shows up here
	})
}

// startMCP starts the local MCP by hand, from its card. The bridge's mcp_start runs in the
// background and wakes no GPU; a click while one start is still running does nothing.
func (a *App) startMCP() {
	if a.mcpStarting {
		return
	}
	if _, busy := a.inflight.Load("mcp-start"); busy {
		return
	}
	a.mcpStarting, a.mcpStartErr = true, ""
	a.once("mcp-start", func() {
		m, err := a.bridge.RunJSON(mcpStartTimeout, "mcp_start")
		res := readMCPStart(m, err)
		a.refreshAccess()
		a.apply(func() { a.finishMCPStart(res) })
	})
}

// finishMCPStart applies one start's outcome to the card and the log.
func (a *App) finishMCPStart(res mcpStart) {
	a.mcpStarting = false
	if res.OK {
		a.mcpStartErr = ""
		a.flash("本机 MCP 桥已启动")
		return
	}
	a.mcpStartErr = res.Error
	a.logf("本机 MCP 桥没能启动：%s", res.Error)
}

func (a *App) ensureKeeper() {
	k := readKeeper(a.paths.Keeper)
	var spawn bool
	done := make(chan struct{})
	a.apply(func() {
		a.keeper = k
		spawn = !k.Alive && a.acct.LoggedIn && a.acct.Active > 0 && a.connected &&
			time.Since(a.lastKeeperSpawn) > 90*time.Second
		if spawn {
			a.lastKeeperSpawn = time.Now()
		}
		close(done)
	})
	<-done
	if !spawn {
		return
	}
	err := a.bridge.SpawnKeeper()
	a.apply(func() {
		if err != nil {
			a.logf("启动后台保活失败：%s", err)
		} else {
			a.logf("已启动后台保活：关掉窗口也不会中断 GPU")
		}
	})
}

// ─── Actions (called from the view, on the main thread) ───────────────────────

func (a *App) setServerURL(u string, byUser bool) {
	u = strings.TrimRight(strings.TrimSpace(u), "/")
	if u != "" {
		a.userClearedURL = false
	} else if byUser {
		a.userClearedURL = true
	}
	if u == a.serverURL {
		return
	}
	a.serverURL, a.urlDraft, a.failures = u, u, 0
	if u == "" {
		a.connected = false
	}
	_ = a.set.Set(map[string]any{"colabServerURL": u})
}

func (a *App) discover(manual bool) {
	if a.discovering {
		return
	}
	a.discovering, a.lastDiscover = true, time.Now()
	if manual {
		a.userClearedURL = false
		a.logf("正在从云端实例找回服务地址…")
	}
	a.once("discover", func() {
		m, err := a.bridge.RunJSON(90*time.Second, "discover_url")
		a.apply(func() {
			a.discovering = false
			if err != nil {
				a.logf("找回地址失败：%s", shortErr(err))
				return
			}
			if ok, _ := m["ok"].(bool); !ok {
				a.logf("找回地址失败：%s", str(m, "error", "未知错误"))
				return
			}
			if n, ok := m["active_assignments"].(float64); ok {
				a.acct.Active = int(n)
			}
			u, _ := m["url"].(string)
			alive, _ := m["app_alive"].(bool)
			if saved, _ := m["token_saved"].(bool); saved && a.tokenStale {
				a.tokenStale = false
				a.flash("已从云端重新取得控制令牌，请再点一次刚才的操作")
				return
			}
			switch {
			case u != "" && alive && (u != a.serverURL || !a.connected):
				a.setServerURL(u, false)
				a.logf("已从云端实例找回服务地址")
			case manual && a.acct.Active == 0:
				a.flash("账号下没有运行中的实例，点顶栏的「唤醒」申请一台")
			case manual:
				a.flash("实例在运行，但 Colab-yoga 服务端没在跑：点顶栏的「重新部署」会复用这台机器")
			}
		})
	})
}

// noteAuth reacts to a refused control call: the token this PC holds is not the one the
// running server uses (it was redeployed from elsewhere), so fetch the current one.
func (a *App) noteAuth(err error) {
	if errors.Is(err, errUnauthorized) {
		a.tokenStale = true
		a.discover(false)
	}
}

func (a *App) touch() {
	url := a.serverURL
	if url == "" || !a.connected {
		return
	}
	a.once("touch", func() {
		st, err := a.server.Post(url, "/api/touch", map[string]any{})
		a.apply(func() {
			if err != nil {
				a.flash("续时失败：" + shortErr(err))
				a.noteAuth(err)
				return
			}
			a.applyStatus(st)
			a.flash("已续时：闲置倒计时重新开始")
		})
	})
}

func (a *App) openWeb() { a.openURL(a.tsURL) }

func (a *App) openFolder(path string) { a.openURL(path) }

// openURL hands a URL or folder to the shell; tests record it instead.
func (a *App) openURL(u string) {
	if u == "" {
		return
	}
	a.opened = append(a.opened, u)
	if a.live {
		go func() { _ = mygo.Shell.OpenExternal(u) }()
	}
}

// switchTo loads id on :8081, replacing whatever runs there (the server stops it first).
func (a *App) switchTo(id string) {
	url := a.serverURL
	if url == "" || !a.connected || a.switching != "" {
		return
	}
	a.switching, a.switchAt = id, a.now()
	doing := "正在加载 "
	if _, _, online := a.servedModel(); online {
		doing = "正在切换到 "
	}
	a.flash(doing + a.modelName(id) + "，下载并装载要几分钟")
	ctx := a.ctxK * 1024
	hf := a.hfToken
	a.once("switch", func() {
		st, err := a.server.Post(url, "/api/load_model", map[string]any{
			"model_id": id, "context_length": ctx, "hf_token": hf,
		})
		a.apply(func() {
			if err != nil {
				a.switching = ""
				a.notify(a.modelName(id)+" 加载失败", shortErr(err))
				a.noteAuth(err)
				return
			}
			a.lastModel = id
			_ = a.set.Set(map[string]any{"selectedModelID": id})
			a.applyStatus(st)
		})
	})
}

func (a *App) unload(id string) {
	url := a.serverURL
	if url == "" || !a.connected {
		return
	}
	a.once("unload", func() {
		st, err := a.server.Post(url, "/api/unload_model", map[string]any{"model_id": id})
		a.apply(func() {
			if err != nil {
				a.flash("卸载失败：" + shortErr(err))
				a.noteAuth(err)
				return
			}
			a.applyStatus(st)
			a.flash("已卸载 " + a.modelName(id))
		})
	})
}

func (a *App) setIdle(mins int) {
	a.idleMin = mins
	_ = a.set.Set(map[string]any{"idleTimeoutMinutes": mins})
	url := a.serverURL
	if url == "" || !a.connected {
		return
	}
	a.once("idle", func() {
		st, err := a.server.Post(url, "/api/config_idle", map[string]any{"idle_timeout_minutes": mins})
		a.apply(func() {
			if err == nil {
				a.applyStatus(st)
			} else {
				a.flash("改自动释放时间失败：" + shortErr(err))
				a.noteAuth(err)
			}
		})
	})
}

func (a *App) release() {
	if a.releasing {
		return
	}
	a.releasing = true
	url := a.serverURL
	a.logf("正在释放全部云端实例…")
	a.once("release", func() {
		if url != "" {
			_, _ = a.server.Post(url, "/api/disconnect", map[string]any{})
		}
		m, err := a.bridge.RunJSON(90*time.Second, "stop_all")
		a.apply(func() {
			a.releasing = false
			a.setServerURL("", false)
			a.status, a.connected = nil, false
			a.hist = [3][]float64{}
			if err != nil {
				a.notify("释放失败", shortErr(err))
				return
			}
			a.acct.Active = int(num(m, "active_assignments", 0))
			a.acct.Burn = num(m, "burn_rate_hourly", 0)
			if b := num(m, "paid_balance", -1); b >= 0 {
				a.acct.Balance, a.acctAt = b, a.now()
			}
			a.flash(fmt.Sprintf("已释放全部实例，当前扣费 %.2f CU/h", a.acct.Burn))
		})
	})
}

func (a *App) wake() {
	if a.waking {
		return
	}
	a.waking = true
	a.wakeAt, a.wakeStage, a.wakeLine, a.wakeErr = a.now(), 0, "", ""
	a.redeploying = a.acct.Active > 0
	gpu := a.gpu
	a.logf("开始唤醒 Colab %s 实例并部署服务端…", gpu)
	a.once("wake", func() {
		// A failed wake exits non-zero after printing WAKE_ERROR; the exit status then only
		// repeats what the user was already told.
		reported := false
		err := a.bridge.Stream(func(line string) {
			a.apply(func() {
				switch {
				case strings.HasPrefix(line, "WAKE_URL:"):
					a.setServerURL(strings.TrimSpace(strings.TrimPrefix(line, "WAKE_URL:")), false)
					a.wakeStage, a.wakeLine = len(wakeStages)-1, "云端服务已就绪"
					a.logf("云端服务已就绪")
				case strings.HasPrefix(line, "WAKE_ERROR:"):
					reported = true
					a.wakeErr = strings.TrimSpace(strings.TrimPrefix(line, "WAKE_ERROR:"))
					a.notify("唤醒失败", a.wakeErr)
				default:
					a.logf("%s", strings.TrimSpace(strings.TrimPrefix(line, "WAKE_LOG:")))
					// The card shows the wake's own progress lines. Whatever else the process
					// prints (warnings, a traceback) is for the log alone.
					if shown := cleanWakeLine(line); strings.HasPrefix(line, "WAKE_LOG:") || wakeStageOf(shown) >= 0 {
						a.noteWake(shown)
					}
				}
			})
		}, "wake", "--gpu", gpu)
		// Connect before the wake is called done, so the window goes from the wake's last
		// stage to the connected server without showing "no instance" in between.
		var url string
		read := make(chan struct{})
		a.apply(func() { url = a.serverURL; close(read) })
		<-read
		if err == nil && url != "" {
			a.pollStatus(url)
		}
		a.apply(func() {
			a.waking = false
			if err != nil && !reported {
				a.wakeErr = err.Error()
				a.notify("唤醒失败", a.wakeErr)
			}
		})
		a.refreshAccount()
	})
}

func (a *App) setGPU(g string) {
	a.gpu = g
	_ = a.set.Set(map[string]any{"wakeGPUChoiceV2": g})
}

func (a *App) setTheme(name string) {
	a.themeName = name
	_ = a.set.Set(map[string]any{"consoleTheme": name})
}

func (a *App) saveAccessURLs() {
	a.tsURL = strings.TrimSuffix(strings.TrimRight(strings.TrimSpace(a.tsURL), "/"), "/v1")
	_ = a.set.Set(map[string]any{"accessTailscaleURL": a.tsURL, "accessMcpURL": a.mcpURL, "accessNovelRoot": a.novelRoot})
	a.once("access", a.refreshAccess)
}

// ─── Derived values for the view ──────────────────────────────────────────────

// servedModel is what llama-server :8081 runs now: from the server's status, else the
// tailnet probe. The alias llama-server reports is the ground truth; an id the server
// guessed for an alias this console's list does not know (an older server did) is dropped.
func (a *App) servedModel() (id, alias string, online bool) {
	if s := a.status; s != nil && s.LLM != nil && s.LLM.Online && a.connected {
		alias, id = s.LLM.Alias, s.LLM.ModelID
		if m := a.modelByAlias(alias); m != nil {
			id = m.ID
		} else if m := a.modelByID(id); m != nil && m.Alias != "" && alias != "" {
			id = ""
		}
		return id, alias, true
	}
	// A connected server probes :8081 on its own machine. When it says nothing serves, the
	// probe over the tailnet, up to ten seconds old, is not asked: it would call a model
	// that was just unloaded or released still online.
	if a.connected && a.status != nil && a.status.LLM != nil {
		return "", "", false
	}
	if a.access.Llama.OK {
		alias = a.access.Llama.Alias
		if m := a.modelByAlias(alias); m != nil {
			id = m.ID
		}
		return id, alias, true
	}
	return "", "", false
}

func homeDir() string {
	if h, err := userHome(); err == nil {
		return h
	}
	return "."
}
