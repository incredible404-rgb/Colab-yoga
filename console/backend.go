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
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
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
	// Runner scripts (real keys): the install's copy and the legacy path that
	// colab-gpu-llm-service's colab_kernel.py exports to; the newer wins.
	Runners []string
}

func findPaths() Paths {
	local := os.Getenv("LOCALAPPDATA")
	if local == "" {
		home, _ := os.UserHomeDir()
		local = filepath.Join(home, "AppData", "Local")
	}
	appDir := filepath.Join(local, "Programs", "Colab-yoga")
	// The exe's own dir when it is an install; a copy in the legacy shim dir has no
	// bridge\ next to it and falls back to the real install.
	if exe, err := os.Executable(); err == nil {
		if d := filepath.Dir(exe); fileExists(filepath.Join(d, "bridge", "colab_yoga_bridge.py")) {
			appDir = d
		}
	}
	return pathsFor(local, appDir)
}

func pathsFor(local, appDir string) Paths {
	cfg := filepath.Join(local, "Colab-yoga")
	return Paths{
		AppDir:    appDir,
		Python:    filepath.Join(appDir, "runtime", "python.exe"),
		PythonW:   filepath.Join(appDir, "runtime", "pythonw.exe"),
		Bridge:    filepath.Join(appDir, "bridge", "colab_yoga_bridge.py"),
		ConfigDir: cfg,
		Settings:  filepath.Join(cfg, "settings.json"),
		Keeper:    filepath.Join(cfg, "keeper.json"),
		Runners: []string{
			filepath.Join(appDir, "colab_kernel_runners.json"),
			filepath.Join(local, "Programs", "EasyColab", "_internal", "colab_kernel_runners.json"),
		},
	}
}

// runnersFile is the newer of the candidate runner files, "" if none exists.
func (p Paths) runnersFile() string {
	best, bestT := "", time.Time{}
	for _, f := range p.Runners {
		if st, err := os.Stat(f); err == nil && st.ModTime().After(bestT) {
			best, bestT = f, st.ModTime()
		}
	}
	return best
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
	Listening bool   `json:"listening"`
	Owner     string `json:"owner"`
	Reachable *bool  `json:"reachable"`
	Error     string `json:"error"`
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

type Server struct{ http *http.Client }

func newServer() *Server {
	return &Server{http: &http.Client{Timeout: 25 * time.Second}}
}

func (s *Server) do(req *http.Request) (*Status, error) {
	resp, err := s.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
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
	GatewayUp                *bool
	Models                   []string // the provider's model list (never its key)
}

type Access struct {
	At     time.Time
	Llama  LlamaProbe
	Magpie MagpieProbe
	MCPUp  bool
	MCPErr string
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
	home, _ := os.UserHomeDir()
	exe := filepath.Join(home, ".local", "bin", "magpie.exe")
	if !fileExists(exe) {
		if p, err := exec.LookPath("magpie"); err == nil {
			exe = p
		} else {
			gwState = nil
			return nil
		}
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
		host := "127.0.0.1:3426"
		if u, err := url.Parse(mcpURL); err == nil && u.Host != "" {
			host = u.Host
		}
		if conn, err := net.DialTimeout("tcp", host, 1500*time.Millisecond); err == nil {
			conn.Close()
			a.MCPUp = true
		} else {
			a.MCPErr = shortErr(err)
		}
	}()
	a.Magpie = findMagpie(tsURL)
	if a.Magpie.ProviderID != "" {
		a.Magpie.GatewayUp = magpieGatewayUp()
	}
	wg.Wait()
	if a.Magpie.ProviderID != "" {
		a.Magpie.Listed = a.Magpie.lists(a.Llama.Alias)
	}
	return a
}

// defaultTailscaleURL finds the Tailscale Service host the colab_kernel runners publish.
func defaultTailscaleURL(p Paths) string {
	b, err := os.ReadFile(p.runnersFile())
	if err != nil {
		return ""
	}
	m := regexp.MustCompile(`\b([a-z0-9-]+\.tail[a-z0-9]+\.ts\.net)\b`).FindSubmatch(b)
	if m == nil {
		return ""
	}
	return "http://" + string(m[1]) + ":443"
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
