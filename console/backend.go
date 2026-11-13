package main

// The console is a view over the other layers (docs/architecture.md); it reimplements
// none of them:
//   - server/colab_yoga_server.py on the Colab VM: /api/* (status, load/unload, idle, touch).
//   - bridge/colab_yoga_bridge.py run by the bundled Python: Google account, wake, URL
//     recovery, release, and the detached keeper that keeps the runtime alive without a GUI.
//   - %LOCALAPPDATA%\Colab-yoga\settings.json.

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"
)

// ─── Paths ────────────────────────────────────────────────────────────────────

type Paths struct {
	AppDir    string // install dir: Colab-yoga.exe, runtime\, site-packages\, bridge\, server\
	Python    string
	PythonW   string
	Bridge    string
	ConfigDir string // %LOCALAPPDATA%\Colab-yoga
	Settings  string
	Keeper    string
	Ledger    string // the official balance as the keeper samples it (colab_yoga_bridge.py ledger_observe)
	Token     string // the server's control token, written by the bridge (wake, discover_url)
	// Runner scripts with real keys, exported by colab-gpu-llm-service's colab_kernel.py.
	// Never part of a release package. The first that exists is used; the order matches
	// resolve_runners_path in bridge/colab_yoga_bridge.py.
	Runners []string
}

func defaultLocalDir(goos, home, envLocal string) string {
	if envLocal != "" {
		return envLocal
	}
	switch goos {
	case "darwin":
		return filepath.Join(home, "Library", "Application Support")
	case "windows":
		return filepath.Join(home, "AppData", "Local")
	default:
		if xdg := os.Getenv("XDG_DATA_HOME"); xdg != "" {
			return xdg
		}
		return filepath.Join(home, ".local", "share")
	}
}

func findPaths() Paths {
	home, _ := os.UserHomeDir()
	local := defaultLocalDir(runtime.GOOS, home, os.Getenv("LOCALAPPDATA"))
	exeDir := ""
	if exe, err := os.Executable(); err == nil {
		if realExe, err := filepath.EvalSymlinks(exe); err == nil {
			exe = realExe
		}
		exeDir = filepath.Dir(exe)
	}
	return pathsFor(local, appDirFor(local, exeDir))
}

// appDirFor is the exe's own directory (or on macOS, the directory holding Colab-yoga.app)
// when it holds the bridge (an installed or an unzipped release package, which runs wherever
// it was unpacked), else the standard install, so a dev build run from console/dist still
// finds its bridge and runtime.
func appDirFor(local, exeDir string) string {
	return appDirForOS(runtime.GOOS, local, exeDir)
}

func appDirForOS(goos, local, exeDir string) string {
	if exeDir != "" {
		if fileExists(filepath.Join(exeDir, "bridge", "colab_yoga_bridge.py")) {
			return exeDir
		}
		if goos == "darwin" && filepath.Base(exeDir) == "MacOS" {
			contentsDir := filepath.Dir(exeDir)
			bundleDir := filepath.Dir(contentsDir)
			if filepath.Base(contentsDir) == "Contents" && strings.HasSuffix(bundleDir, ".app") {
				parent := filepath.Dir(bundleDir)
				if fileExists(filepath.Join(parent, "bridge", "colab_yoga_bridge.py")) {
					return parent
				}
			}
		}
	}
	return filepath.Join(local, "Programs", "Colab-yoga")
}

func pathsFor(local, appDir string) Paths {
	return pathsForOS(runtime.GOOS, local, appDir)
}

func pathsForOS(goos, local, appDir string) Paths {
	cfg := filepath.Join(local, "Colab-yoga")
	py := filepath.Join(appDir, "runtime", "bin", "python3")
	pyw := py
	if goos == "windows" {
		py = filepath.Join(appDir, "runtime", "python.exe")
		pyw = filepath.Join(appDir, "runtime", "pythonw.exe")
	}
	return Paths{
		AppDir:    appDir,
		Python:    py,
		PythonW:   pyw,
		Bridge:    filepath.Join(appDir, "bridge", "colab_yoga_bridge.py"),
		ConfigDir: cfg,
		Settings:  filepath.Join(cfg, "settings.json"),
		Keeper:    filepath.Join(cfg, "keeper.json"),
		Ledger:    filepath.Join(cfg, "ledger.json"),
		Token:     filepath.Join(cfg, "server_token.json"),
		Runners:   runnersCandidates(local, appDir, cfg),
	}
}

// runnersCandidates: $COLAB_YOGA_RUNNERS, $COLAB_YOGA_DIR, next to the exe, the config
// dir (where colab_kernel.py writes it by default, so an unzipped package finds it
// anywhere), then the standard install. runnersFile takes the first that exists.
func runnersCandidates(local, appDir, cfg string) []string {
	const name = "colab_kernel_runners.json"
	var out []string
	if f := os.Getenv("COLAB_YOGA_RUNNERS"); f != "" {
		out = append(out, f)
	}
	if d := os.Getenv("COLAB_YOGA_DIR"); d != "" {
		out = append(out, filepath.Join(d, name))
	}
	return append(out,
		filepath.Join(appDir, name),
		filepath.Join(cfg, name),
		filepath.Join(local, "Programs", "Colab-yoga", name),
	)
}

// writeInstallAnchor records where this exe runs from in %LOCALAPPDATA%\Colab-yoga\install.json,
// so colab-gpu-llm-service can find an unzipped package. Best effort: a failure is ignored.
func writeInstallAnchor(p Paths) {
	if p.ConfigDir == "" || p.AppDir == "" {
		return
	}
	abs, err := filepath.Abs(p.AppDir)
	if err != nil {
		return
	}
	b, _ := json.MarshalIndent(map[string]string{"appDir": abs}, "", "  ")
	if os.MkdirAll(p.ConfigDir, 0o755) == nil {
		_ = os.WriteFile(filepath.Join(p.ConfigDir, "install.json"), append(b, '\n'), 0o644)
	}
}

// defaultNovelRoot is the MCP card's working folder before one is set: the local MCP's own
// cwd (~/.config/colab-cli/local-mcp.json), else ~/Documents/Colab创作 when it exists, else
// ~/Documents.
func defaultNovelRoot(home string) string {
	if b, err := os.ReadFile(filepath.Join(home, ".config", "colab-cli", "local-mcp.json")); err == nil {
		var cfg struct {
			Cwd string `json:"cwd"`
		}
		if json.Unmarshal(b, &cfg) == nil && strings.TrimSpace(cfg.Cwd) != "" {
			return cfg.Cwd
		}
	}
	if d := filepath.Join(home, "Documents", "Colab创作"); fileExists(d) {
		return d
	}
	return filepath.Join(home, "Documents")
}

// runnersFile is the first runner script file that exists, "" if none does.
func (p Paths) runnersFile() string {
	for _, f := range p.Runners {
		if fileExists(f) {
			return f
		}
	}
	return ""
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// ─── Settings (read-merge-write, so keys of other versions survive) ──────────

type Settings struct {
	path string
	mu   sync.Mutex
}

func (s *Settings) Load() map[string]any {
	m := map[string]any{}
	if b, err := os.ReadFile(s.path); err == nil {
		_ = json.Unmarshal(b, &m)
	}
	return m
}

func (s *Settings) Set(kv map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	m := map[string]any{}
	if b, err := os.ReadFile(s.path); err == nil {
		// Another writer may be mid-write; never write back a half-read copy, which
		// would drop every key it had.
		if err := json.Unmarshal(b, &m); err != nil {
			return fmt.Errorf("settings.json unreadable, not written: %w", err)
		}
	}
	for k, v := range kv {
		m[k] = v
	}
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	tmp := s.path + ".console.tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func str(m map[string]any, key, def string) string {
	if v, ok := m[key].(string); ok && strings.TrimSpace(v) != "" {
		return strings.TrimSpace(v)
	}
	return def
}

func num(m map[string]any, key string, def float64) float64 {
	if v, ok := m[key].(float64); ok {
		return v
	}
	return def
}

// ─── Colab-yoga server client ─────────────────────────────────────────────────

type MCPBridge struct {
	Configured *bool  `json:"configured"` // nil from a server older than the field: see forwards
	Listening  bool   `json:"listening"`
	Owner      string `json:"owner"`
	Reachable  *bool  `json:"reachable"`
	Error      string `json:"error"`
}

// forwards: the server has a local_tailscale_ip to forward the MCP to. A server older than
// the configured field only ever tried, listened, owned or failed when it had one.
func (b *MCPBridge) forwards() bool {
	if b.Configured != nil {
		return *b.Configured
	}
	return b.Listening || b.Owner != "" || b.Error != "" || b.Reachable != nil
}

type LLMServer struct {
	Online        bool       `json:"online"`
	Alias         string     `json:"alias"`
	ModelID       string     `json:"model_id"`
	SlotsTotal    int        `json:"slots_total"`
	SlotsBusy     int        `json:"slots_busy"`
	NCtx          int        `json:"n_ctx_per_slot"`
	LastActiveAge int        `json:"last_active_age_s"`
	MCP           *MCPBridge `json:"mcp_bridge"`
}

type Status struct {
	ServerVersion  string     `json:"server_version"`
	GPUName        string     `json:"gpu_name"`
	VRAMUsed       float64    `json:"vram_used_gb"`
	VRAMTotal      float64    `json:"vram_total_gb"`
	RAMUsed        float64    `json:"ram_used_gb"`
	RAMTotal       float64    `json:"ram_total_gb"`
	DiskUsed       float64    `json:"disk_used_gb"`
	DiskTotal      float64    `json:"disk_total_gb"`
	Uptime         float64    `json:"uptime_seconds"`
	SessionCU      float64    `json:"session_cu_used"`
	Burn           float64    `json:"cu_burn_rate_per_hour"`
	IdleTimeoutMin float64    `json:"idle_timeout_minutes"`
	IdleRemaining  float64    `json:"idle_remaining_seconds"`
	LoadedIDs      []string   `json:"loaded_model_ids"`
	IsDownloading  bool       `json:"is_downloading"`
	IsLoading      bool       `json:"is_loading_vram"`
	TaskProgress   float64    `json:"task_progress"`
	TaskMessage    string     `json:"task_message"`
	Console        []string   `json:"console_lines"`
	LLM            *LLMServer `json:"llm_server"`
	ContextLength  float64    `json:"context_length"`
}

// errUnauthorized is a 401 from the server: the control token is missing or belongs to an
// earlier deployment. discover_url fetches the current one from the VM.
var errUnauthorized = errors.New("HTTP 401")

// Server talks to the Colab-yoga server's HTTP API. Every call but GET /api/status needs
// the Bearer token the bridge stored in tokenPath (wake, discover_url); a server older
// than the token ignores the header, so sending it is always safe.
type Server struct {
	http      *http.Client
	tokenPath string
}

func newServer(tokenPath string) *Server {
	return &Server{http: &http.Client{Timeout: 25 * time.Second}, tokenPath: tokenPath}
}

// token is read from the file on every call: a wake or a discover writes a new one
// while the console runs.
func (s *Server) token() string {
	if s.tokenPath == "" {
		return ""
	}
	b, err := os.ReadFile(s.tokenPath)
	if err != nil {
		return ""
	}
	var m struct {
		Token string `json:"token"`
	}
	if json.Unmarshal(b, &m) != nil {
		return ""
	}
	return strings.TrimSpace(m.Token)
}

func (s *Server) do(req *http.Request) (*Status, error) {
	resp, err := s.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized {
		return nil, errUnauthorized
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var st Status
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		return nil, err
	}
	return &st, nil
}

func (s *Server) Status(base, modelID string) (*Status, error) {
	u := strings.TrimRight(base, "/") + "/api/status"
	if modelID != "" {
		u += "?model_id=" + url.QueryEscape(modelID)
	}
	req, err := http.NewRequest("GET", u, nil)
	if err != nil {
		return nil, err
	}
	return s.do(req)
}

func (s *Server) Post(base, path string, body any) (*Status, error) {
	b, _ := json.Marshal(body)
	req, err := http.NewRequest("POST", strings.TrimRight(base, "/")+path, bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if t := s.token(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t)
	}
	return s.do(req)
}

// ─── Colab-yoga bridge ────────────────────────────────────────────────────────

type Bridge struct{ p Paths }

func (b *Bridge) cmd(python string, args ...string) *exec.Cmd {
	cmd := exec.Command(python, append([]string{"-X", "utf8", b.p.Bridge}, args...)...)
	cmd.Env = append(os.Environ(), "PYTHONUTF8=1", "PYTHONIOENCODING=utf-8")
	cmd.Dir = b.p.AppDir
	hideWindow(cmd)
	return cmd
}

// RunJSON runs a bridge subcommand that prints one JSON object as its last line.
func (b *Bridge) RunJSON(timeout time.Duration, args ...string) (map[string]any, error) {
	cmd := b.cmd(b.p.Python, args...)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(timeout):
		_ = cmd.Process.Kill()
		return nil, errors.New("bridge timed out")
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		l := strings.TrimSpace(lines[i])
		if strings.HasPrefix(l, "{") && strings.HasSuffix(l, "}") {
			m := map[string]any{}
			if err := json.Unmarshal([]byte(l), &m); err == nil {
				return m, nil
			}
		}
	}
	tail := out.String()
	if len(tail) > 300 {
		tail = tail[len(tail)-300:]
	}
	return nil, fmt.Errorf("bridge gave no JSON: %s", strings.TrimSpace(tail))
}

// Stream runs a long subcommand (wake) and hands each output line to onLine. A wake
// takes a few minutes at most; past the limit the process is killed.
func (b *Bridge) Stream(onLine func(string), args ...string) error {
	cmd := b.cmd(b.p.Python, args...)
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	if err := cmd.Start(); err != nil {
		return err
	}
	waited := make(chan error, 1)
	go func() { waited <- cmd.Wait(); pw.Close() }()
	timer := time.AfterFunc(15*time.Minute, func() { _ = cmd.Process.Kill() })
	defer timer.Stop()
	sc := bufio.NewScanner(pr)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		if l := strings.TrimSpace(sc.Text()); l != "" {
			onLine(l)
		}
	}
	if err := sc.Err(); err != nil {
		return err
	}
	if err := <-waited; err != nil {
		return fmt.Errorf("bridge exited: %w", err)
	}
	return nil
}

// SpawnKeeper starts the detached heartbeat; it outlives this process.
func (b *Bridge) SpawnKeeper() error {
	py := b.p.PythonW
	if !fileExists(py) {
		py = b.p.Python
	}
	return startDetached(b.cmd(py, "keeper"))
}

// ─── Keeper status (written by colab_yoga_bridge.py keeper) ────────────────────────

type Keeper struct {
	Alive      bool
	Beats      int
	LastBeat   time.Time
	ExitReason string
}

func readKeeper(path string) Keeper {
	m := map[string]any{}
	b, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(b, &m) != nil {
		return Keeper{}
	}
	k := Keeper{Beats: int(num(m, "beats", 0)), ExitReason: str(m, "exit_reason", "")}
	if ts := num(m, "last_beat_ts", 0); ts > 0 {
		k.LastBeat = time.Unix(int64(ts), 0)
	}
	k.Alive = k.ExitReason == "" && pidAlive(int(num(m, "pid", 0)))
	return k
}

// ─── Official ledger (ledger.json, written by the bridge's keeper and auth_status) ──────

// Colab books CU about every 5 minutes, each step roughly the advertised rate times the time
// since the previous one, but the steps jitter: one 4.9-minute step came to 10.45 CU/h against
// 10.05 advertised. A measured rate therefore spans at least ledgerMinSteps settlements, over
// the last ledgerWindow, so it follows a rate change within about half an hour.
const (
	ledgerMinSteps = 3
	ledgerWindow   = 35 * time.Minute
	ledgerStale    = 2 * time.Minute // older than this the keeper is not reading it
	// at most two settlement intervals are ever shown as not yet deducted
	ledgerPendingCap = 10 * time.Minute
)

type LedgerPoint struct {
	At      time.Time
	Balance float64
}

// LedgerSession is one billing stretch (assignments > 0). Exact: StartBalance is the balance
// read with nothing assigned right before it, so StartBalance - Balance is everything this
// session was charged; otherwise the charges before StartAt are unknown.
type LedgerSession struct {
	StartBalance float64
	StartAt      time.Time
	Exact        bool
	Ended        bool
	Points       []LedgerPoint // readings where the balance dropped: observed settlements
}

type Ledger struct {
	Balance      float64
	CheckedAt    time.Time
	Active       int
	OfficialRate float64
	PollEvery    time.Duration
	Session      *LedgerSession
}

func unixTime(v float64) time.Time {
	if v <= 0 {
		return time.Time{}
	}
	return time.Unix(0, int64(v*float64(time.Second)))
}

func readLedger(path string) Ledger {
	m := map[string]any{}
	b, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(b, &m) != nil {
		return Ledger{}
	}
	l := Ledger{Balance: num(m, "balance", 0), CheckedAt: unixTime(num(m, "checked_ts", 0)),
		Active: int(num(m, "active", 0)), OfficialRate: num(m, "official_rate", 0),
		PollEvery: time.Duration(num(m, "poll_s", 15) * float64(time.Second))}
	if s, ok := m["session"].(map[string]any); ok {
		ls := &LedgerSession{StartBalance: num(s, "start_balance", 0), StartAt: unixTime(num(s, "start_ts", 0)),
			Ended: num(s, "ended_ts", 0) > 0}
		ls.Exact, _ = s["exact"].(bool)
		if pts, ok := s["points"].([]any); ok {
			for _, p := range pts {
				if pair, ok := p.([]any); ok && len(pair) == 2 {
					ts, _ := pair[0].(float64)
					bal, _ := pair[1].(float64)
					ls.Points = append(ls.Points, LedgerPoint{At: unixTime(ts), Balance: bal})
				}
			}
		}
		l.Session = ls
	}
	return l
}

// Live: a billing session the keeper is still reading.
func (l Ledger) Live(now time.Time) bool {
	return l.Session != nil && !l.Session.Ended && l.Active > 0 && !l.CheckedAt.IsZero() &&
		now.Sub(l.CheckedAt) < ledgerStale
}

// Spent is what the official ledger has deducted this session so far.
func (l Ledger) Spent() float64 {
	if l.Session == nil {
		return 0
	}
	return max(0, l.Session.StartBalance-l.Balance)
}

// LastSettle is when a deduction was last seen, zero before the first one.
func (l Ledger) LastSettle() time.Time {
	if l.Session == nil || len(l.Session.Points) == 0 {
		return time.Time{}
	}
	return l.Session.Points[len(l.Session.Points)-1].At
}

// MeasuredRate is CU/h from the balance itself: the drop from the first to the last settlement
// in the window over the time between them, and how many settlements that spans. ok is false
// until the window holds ledgerMinSteps of them.
func (l Ledger) MeasuredRate() (rate float64, steps int, ok bool) {
	if l.Session == nil || len(l.Session.Points) < ledgerMinSteps+1 {
		return 0, 0, false
	}
	pts := l.Session.Points
	last := pts[len(pts)-1]
	first := len(pts) - 1
	for first > 0 && last.At.Sub(pts[first-1].At) <= ledgerWindow {
		first--
	}
	steps = len(pts) - 1 - first
	hours := last.At.Sub(pts[first].At).Hours()
	if steps < ledgerMinSteps || hours <= 0 {
		return 0, steps, false
	}
	rate = (pts[first].Balance - last.Balance) / hours
	return rate, steps, rate > 0
}

// ─── Access probes (what the user actually chats through) ─────────────────────

type LlamaProbe struct {
	OK         bool
	Alias      string
	NCtx       int
	SlotsTotal int
	SlotsBusy  int
	Err        string
}

type MagpieProbe struct {
	ProviderID, ProviderName string
	Off, Listed              bool
	Installed                bool // a Magpie providers.json or the magpie command exists
	GatewayUp                *bool
	Models                   []string // the provider's model list (never its key)
}

// mcpStartTimeout covers the helper's own 8 s readiness wait with room to spare.
const mcpStartTimeout = 30 * time.Second

// mcpStart is the bridge's answer to mcp_start. State is started, running, failed or absent.
type mcpStart struct {
	OK    bool
	State string
	Error string
}

// readMCPStart reads that answer. A call that gave no answer, or a failure without a reason,
// is still a failure the card can say something about.
func readMCPStart(m map[string]any, err error) mcpStart {
	if err != nil {
		return mcpStart{Error: "启动命令没有返回结果：" + err.Error()}
	}
	ok, _ := m["ok"].(bool)
	res := mcpStart{OK: ok, State: str(m, "state", ""), Error: str(m, "error", "")}
	if !res.OK && res.Error == "" {
		res.Error = "未说明原因"
	}
	return res
}

type Access struct {
	At      time.Time
	Llama   LlamaProbe
	Magpie  MagpieProbe
	MCPUp   bool
	MCPName string // the name the local MCP's /health reports, when it answers
	MCPErr  string
}

// mcpName is how the local MCP identifies itself in /health, agreed with the
// colab-gpu-llm-service side: {"ok": true, "server": "local_mcp", ...}.
const mcpName = "local_mcp"

// probeMCP asks the local MCP's /health. A listening port proves nothing: any program
// can hold 3426, so only the name the service reports counts as the MCP being up. A
// service that answers without a name, or not at all, is reported as what it is.
func probeMCP(mcpURL string) (up bool, name, errText string) {
	scheme, host := "http", "127.0.0.1:3426"
	if u, err := url.Parse(strings.TrimSpace(mcpURL)); err == nil && u.Host != "" {
		if u.Scheme != "" {
			scheme = u.Scheme
		}
		host = u.Host
	}
	resp, err := tailnet.Get(scheme + "://" + host + "/health")
	if err != nil {
		return false, "", shortErr(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode != http.StatusOK {
		return false, "", fmt.Sprintf("端口上有服务但 /health 返回 %d：可能是旧版本机 MCP", resp.StatusCode)
	}
	var h struct {
		OK     bool   `json:"ok"`
		Server string `json:"server"`
		Name   string `json:"name"` // the older spelling of server, still accepted
	}
	_ = json.Unmarshal(body, &h) // a body that is not JSON reports no name, handled below
	id := strings.TrimSpace(h.Server)
	if id == "" {
		id = strings.TrimSpace(h.Name)
	}
	if id == "" {
		return false, "", "端口上有服务，但 /health 没报出服务名，不是本机 MCP"
	}
	if !strings.EqualFold(id, mcpName) {
		return false, id, fmt.Sprintf("端口上是其他服务（server=%s），不是本机 MCP", id)
	}
	if !h.OK {
		return false, id, "本机 MCP 的 /health 报告未就绪（ok 不是 true）"
	}
	return true, id, ""
}

// Tailnet hosts must not go through a system HTTP proxy.
var tailnet = &http.Client{Timeout: 4 * time.Second, Transport: &http.Transport{Proxy: nil}}

func getJSON(u string, v any) error {
	resp, err := tailnet.Get(u)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(v)
}

func probeLlama(base string) LlamaProbe {
	var p LlamaProbe
	base = strings.TrimRight(base, "/")
	if base == "" {
		p.Err = "未配置"
		return p
	}
	var health map[string]any
	if err := getJSON(base+"/health", &health); err != nil {
		p.Err = shortErr(err)
		return p
	}
	p.OK = true
	// The model and the slots are independent round trips over the tailnet: ask both at
	// once, so a probe costs one round trip after /health rather than two.
	var models struct {
		Data []struct {
			ID   string `json:"id"`
			Meta struct {
				NCtx int `json:"n_ctx"`
			} `json:"meta"`
		} `json:"data"`
	}
	var slots []struct {
		Busy bool `json:"is_processing"`
	}
	var wg sync.WaitGroup
	var modelsErr, slotsErr error
	wg.Add(2)
	go func() { defer wg.Done(); modelsErr = getJSON(base+"/v1/models", &models) }()
	go func() { defer wg.Done(); slotsErr = getJSON(base+"/slots", &slots) }()
	wg.Wait()
	if modelsErr == nil && len(models.Data) > 0 {
		p.Alias, p.NCtx = models.Data[0].ID, models.Data[0].Meta.NCtx
	}
	if slotsErr == nil {
		p.SlotsTotal = len(slots)
		for _, s := range slots {
			if s.Busy {
				p.SlotsBusy++
			}
		}
	}
	return p
}

func shortErr(err error) string {
	s := err.Error()
	switch {
	case strings.Contains(s, "connection reset"), strings.Contains(s, "forcibly closed"):
		return "连接被重置"
	case strings.Contains(s, "timeout"), strings.Contains(s, "deadline"):
		return "超时"
	case strings.Contains(s, "refused"):
		return "拒绝连接"
	case errors.Is(err, errUnauthorized):
		return "控制令牌无效，正在从云端重新取"
	}
	if len(s) > 60 {
		s = s[:60]
	}
	return s
}

// findMagpie reads the Magpie provider whose chat URL is the tailnet /v1. It never
// reads or returns the provider's key.
func findMagpie(tsURL string) MagpieProbe {
	var out MagpieProbe
	home, _ := os.UserHomeDir()
	b, err := magpieProviders.read(filepath.Join(home, ".config", "magpie", "providers.json"))
	out.Installed = err == nil
	if err != nil || tsURL == "" {
		return out
	}
	var doc struct {
		Providers []map[string]any `json:"providers"`
	}
	if json.Unmarshal(b, &doc) != nil {
		return out
	}
	target := strings.TrimRight(tsURL, "/")
	for _, p := range doc.Providers {
		for _, k := range []string{"chat", "url", "baseURL", "base"} {
			if v, _ := p[k].(string); v != "" && strings.HasPrefix(strings.TrimRight(v, "/"), target) {
				out.ProviderID, _ = p["id"].(string)
				out.ProviderName, _ = p["name"].(string)
				out.Off, _ = p["off"].(bool)
				if ms, ok := p["models"].([]any); ok {
					for _, m := range ms {
						if s, ok := m.(string); ok {
							out.Models = append(out.Models, s)
						}
					}
				}
				return out
			}
		}
	}
	return out
}

// statCache keeps a file's bytes until its size or modification time changes, so a
// probe every 10 s stats the file instead of reading it again.
type statCache struct {
	mu   sync.Mutex
	size int64
	mod  time.Time
	b    []byte
}

var magpieProviders statCache

func (c *statCache) read(path string) ([]byte, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.b != nil && fi.Size() == c.size && fi.ModTime().Equal(c.mod) {
		return c.b, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	c.b, c.size, c.mod = b, fi.Size(), fi.ModTime()
	return b, nil
}

func (m MagpieProbe) lists(alias string) bool {
	for _, s := range m.Models {
		if alias != "" && s == alias {
			return true
		}
	}
	return false
}

var (
	gwMu    sync.Mutex
	gwAt    time.Time
	gwState *bool
)

// magpieGatewayUp runs `magpie healthcheck` (exit 0 when the gateway answers), cached.
func magpieGatewayUp() *bool {
	gwMu.Lock()
	defer gwMu.Unlock()
	if time.Since(gwAt) < time.Minute {
		return gwState
	}
	gwAt = time.Now()
	exe := magpieExe()
	if exe == "" {
		gwState = nil
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, exe, "healthcheck")
	hideWindow(cmd)
	err := cmd.Run()
	if ctx.Err() != nil {
		gwState = nil // a hung CLI says nothing about the gateway
		return nil
	}
	up := err == nil
	gwState = &up
	return gwState
}

// magpieExe is the magpie CLI: ~/.local/bin/magpie(.exe), else on PATH; "" when not installed.
func magpieExe() string {
	home, _ := os.UserHomeDir()
	binName := "magpie"
	if runtime.GOOS == "windows" {
		binName = "magpie.exe"
	}
	if exe := filepath.Join(home, ".local", "bin", binName); fileExists(exe) {
		return exe
	}
	if p, err := exec.LookPath("magpie"); err == nil {
		return p
	}
	return ""
}

// probeAccess checks each way in. The checks do not depend on one another (the tailnet
// probe, the Magpie gateway CLI, the local MCP port), so they run together and a probe
// takes as long as the slowest rather than their sum.
func probeAccess(tsURL, mcpURL string) Access {
	a := Access{At: time.Now()}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); a.Llama = probeLlama(tsURL) }()
	go func() {
		defer wg.Done()
		a.MCPUp, a.MCPName, a.MCPErr = probeMCP(mcpURL)
	}()
	a.Magpie = findMagpie(tsURL)
	if !a.Magpie.Installed {
		a.Magpie.Installed = magpieExe() != ""
	}
	if a.Magpie.ProviderID != "" {
		a.Magpie.GatewayUp = magpieGatewayUp()
	}
	wg.Wait()
	if a.Magpie.ProviderID != "" {
		a.Magpie.Listed = a.Magpie.lists(a.Llama.Alias)
	}
	return a
}

// ─── Region from a Colab endpoint name, e.g. gpu-g4-s-asso1c0-3i88… → asso1 ─────

var regionRe = regexp.MustCompile(`^[a-z]+-[a-z0-9]+-[a-z]-([a-z]+\d+)`)

var regionNames = map[string]string{
	// Seen on G4 runtimes: euw4 (europe-west4), asso1 (asia-southeast1). The rest follow
	// the same abbreviation of Google Cloud region names.
	"euw4": "荷兰", "euw1": "比利时", "euw2": "伦敦", "euw3": "法兰克福", "eun1": "芬兰",
	"asso1": "新加坡", "ase1": "台湾", "asne1": "东京", "asne3": "首尔", "ass1": "孟买",
	"usc1": "美国中部", "use1": "美国东部", "use4": "美国东部", "use5": "美国东部",
	"usw1": "美国西部", "usw4": "美国西部", "uss1": "美国南部",
}

func regionOf(endpoint string) (code, name string) {
	m := regionRe.FindStringSubmatch(endpoint)
	if m == nil {
		return "", ""
	}
	return m[1], regionNames[m[1]]
}
