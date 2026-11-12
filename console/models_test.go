package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/egoist/mygo/ui"
)

// fixtureRunner is one entry of a runners file: slug, the alias llama-server reports,
// the shard sizes, and the boot script (with a fake key, to prove it is never surfaced).
type fixtureRunner struct {
	ID, Alias string
	Sizes     []int64
	FQDN      string
}

const fakeSecret = "sk-FAKE-SECRET-NOT-REAL"

var shippedRunners = []fixtureRunner{
	{"qwen38-huihui-v2", "Huihui-Qwen3.8-Flash-Next-abliterated-v2-UD-Q4_K_XL", []int64{100_000_000_000, 15_000_000_000}, "qwen.tailabc123.ts.net"},
	{"gemma4-31b", "Gemma-4-31B-it-ARA-Heretic-Q8_0", []int64{34_800_000_000}, "qwen.tailabc123.ts.net"},
	{"qwen38-27b-rvn", "Qwen3.8-27B-RVN-Heretic-BF16", []int64{56_100_000_000}, "qwen.tailabc123.ts.net"},
	{"glm53-flash", "GLM-5.3-Flash-Uncensored-GSQ-RCO-Q4", []int64{138_200_000_000}, "qwen.tailabc123.ts.net"},
}

// writeRunners writes a runners file with the entries in this order and returns its path.
func writeRunners(t *testing.T, dir string, runners []fixtureRunner) string {
	t.Helper()
	var parts []string
	for _, r := range runners {
		var files []string
		for i, sz := range r.Sizes {
			files = append(files, fmt.Sprintf(`{"name":"f%d.gguf","size":%d,"role":"main"}`, i, sz))
		}
		code := fmt.Sprintf("API_KEY = '%s'\\nprint('boot')", fakeSecret)
		if r.FQDN != "" {
			code = fmt.Sprintf("SERVICE_FQDN = \\\"%s\\\"\\n", r.FQDN) + code
		}
		parts = append(parts, fmt.Sprintf(`%q:{"slug":%q,"alias":%q,"title":"t","root_dir":"/x","min_vram_mib":1,"files":[%s],"code":"%s"}`,
			r.ID, r.ID, r.Alias, strings.Join(files, ","), code))
	}
	path := filepath.Join(dir, "colab_kernel_runners.json")
	if err := os.WriteFile(path, []byte("{"+strings.Join(parts, ",")+"}"), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestCatalogFollowsTheRunnersFileInOrderWithKnownNames(t *testing.T) {
	path := writeRunners(t, t.TempDir(), shippedRunners)
	runners, err := readRunners(path)
	if err != nil {
		t.Fatal(err)
	}
	cat := catalogFrom(runners)
	if len(cat) != 4 || cat[0].ID != "qwen38-huihui-v2" || cat[3].ID != "glm53-flash" {
		t.Fatalf("catalog order/size: %+v", cat)
	}
	m := cat[0]
	if m.Name != "Qwen3.8-Flash-Next (Huihui v2)" || m.Alias != shippedRunners[0].Alias || len(m.Tags) == 0 || m.Note == "" {
		t.Errorf("a known model keeps its curated name and tags, with the file's alias: %+v", m)
	}
	if m.SizeGB < 114.99 || m.SizeGB > 115.01 {
		t.Errorf("size is the sum of the files: %v", m.SizeGB)
	}
}

func TestAModelTheUserAddedIsListedByAliasWithItsSummedSize(t *testing.T) {
	mine := fixtureRunner{"my-70b", "My-70B-Instruct-Q5_K_M", []int64{20_000_000_000, 21_500_000_000}, ""}
	runners, err := readRunners(writeRunners(t, t.TempDir(), append(append([]fixtureRunner{}, shippedRunners[:1]...), mine)))
	if err != nil {
		t.Fatal(err)
	}
	cat := catalogFrom(runners)
	if len(cat) != 2 || cat[1].ID != "my-70b" || cat[1].Name != "My-70B-Instruct-Q5_K_M" || cat[1].Alias != cat[1].Name ||
		len(cat[1].Tags) != 0 || cat[1].SizeGB < 41.4 || cat[1].SizeGB > 41.6 {
		t.Errorf("unknown model: %+v", cat)
	}
}

func TestTheScriptsKeysAreNeverKept(t *testing.T) {
	runners, err := readRunners(writeRunners(t, t.TempDir(), shippedRunners))
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprintf("%+v %+v", runners, catalogFrom(runners)); strings.Contains(got, fakeSecret) {
		t.Errorf("a runner script leaked into the parsed data: %s", got)
	}
}

func TestOddRunnersFiles(t *testing.T) {
	dir := t.TempDir()
	for name, body := range map[string]string{"array": `[1,2]`, "empty": ``, "junk": `{"a":`} {
		p := filepath.Join(dir, name+".json")
		_ = os.WriteFile(p, []byte(body), 0o644)
		if rs, _ := readRunners(p); len(catalogFrom(rs)) != 0 {
			t.Errorf("%s: %+v", name, rs)
		}
	}
	p := filepath.Join(dir, "mixed.json")
	_ = os.WriteFile(p, []byte(`{"bad":7,"good":{"alias":"A","files":[{"size":2e9}]},"":{"alias":"nameless"}}`), 0o644)
	rs, err := readRunners(p)
	if err != nil || len(rs) != 1 || rs[0].ID != "good" || rs[0].Alias != "A" || rs[0].SizeBytes != 2e9 {
		t.Errorf("entries that are not runners are skipped, the key is the id when there is no slug: %+v %v", rs, err)
	}
}

func TestTailnetHostFromTheRunners(t *testing.T) {
	dir := t.TempDir()
	p := Paths{Runners: []string{writeRunners(t, dir, shippedRunners)}}
	if got := defaultTailscaleURL(p); got != "http://qwen.tailabc123.ts.net:443" {
		t.Errorf("SERVICE_FQDN: %q", got)
	}
	// No SERVICE_FQDN line: any *.ts.net host in the scripts will do, not only *.tailNNN.ts.net.
	only := filepath.Join(dir, "other.json")
	_ = os.WriteFile(only, []byte(`{"m":{"alias":"A","code":"url = 'http://llm.mytailnet.example-corp.ts.net:8081/v1'"}}`), 0o644)
	if got := defaultTailscaleURL(Paths{Runners: []string{only}}); got != "http://llm.mytailnet.example-corp.ts.net:443" {
		t.Errorf("any ts.net host: %q", got)
	}
	// SERVICE_FQDN beats another ts.net host mentioned earlier in the script.
	both := filepath.Join(dir, "both.json")
	_ = os.WriteFile(both, []byte(`{"m":{"alias":"A","code":"# see node.tailabc.ts.net\nSERVICE_FQDN = 'svc.tailabc.ts.net'"}}`), 0o644)
	if got := defaultTailscaleURL(Paths{Runners: []string{both}}); got != "http://svc.tailabc.ts.net:443" {
		t.Errorf("SERVICE_FQDN wins: %q", got)
	}
	none := filepath.Join(dir, "none.json")
	_ = os.WriteFile(none, []byte(`{"m":{"alias":"A","code":"print(1)"}}`), 0o644)
	if got := defaultTailscaleURL(Paths{Runners: []string{none}}); got != "" {
		t.Errorf("nothing to recognise: %q", got)
	}
	if got := defaultTailscaleURL(Paths{}); got != "" {
		t.Errorf("no runners file: %q", got)
	}
}

func TestAccessAddressFromRunnersFillsOnlyAnEmptyOne(t *testing.T) {
	a := fakeApp(t)
	if a.tsURL != "http://qwen.example.ts.net:443" { // fakeApp sets its own after newApp
		t.Fatalf("setup: %q", a.tsURL)
	}
	b := newApp(a.paths)
	if b.tsURL != "http://qwen.tailabc123.ts.net:443" {
		t.Errorf("a new console takes the host the runners name: %q", b.tsURL)
	}
	_ = b.set.Set(map[string]any{"accessTailscaleURL": "http://typed.by.user.ts.net:443"})
	c := newApp(a.paths)
	if c.tsURL != "http://typed.by.user.ts.net:443" {
		t.Errorf("what the user typed wins: %q", c.tsURL)
	}
}

func TestCatalogReloadsWhenTheFileChangesAndEmptiesWhenItGoes(t *testing.T) {
	a := fakeApp(t)
	if len(a.catalog) != 4 {
		t.Fatalf("start: %d models", len(a.catalog))
	}
	path := a.paths.Runners[0]
	writeRunners(t, filepath.Dir(path), append(append([]fixtureRunner{}, shippedRunners...),
		fixtureRunner{"my-70b", "My-70B", []int64{1_000_000_000}, ""}))
	later := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(path, later, later)
	a.reloadCatalog()
	if len(a.catalog) != 5 || a.modelByAlias("My-70B") == nil {
		t.Errorf("the new model should show: %+v", a.catalog)
	}
	_ = os.Remove(path)
	a.reloadCatalog()
	if len(a.catalog) != 0 {
		t.Errorf("no runners file, no models: %+v", a.catalog)
	}
}

func TestServedModelTrustsTheAliasOverAnIdTheServerGuessed(t *testing.T) {
	a := fakeApp(t)
	// An older server reported the first known model for an alias it did not know.
	a.status.LLM.Alias, a.status.LLM.ModelID = "Totally-Other-Model", "qwen38-huihui-v2"
	id, alias, online := a.servedModel()
	if id != "" || alias != "Totally-Other-Model" || !online {
		t.Errorf("unknown alias must not become a known model: %q %q %v", id, alias, online)
	}
	// A model the user added is found by its alias even if the server sent no id.
	a.catalog = append(a.catalog, Model{ID: "my-70b", Name: "My-70B", Alias: "My-70B-Q5"})
	a.status.LLM.Alias, a.status.LLM.ModelID = "My-70B-Q5", ""
	if id, _, _ := a.servedModel(); id != "my-70b" {
		t.Errorf("alias lookup: %q", id)
	}
	// With no runners file here, the server's own id is all there is, and it stands.
	a.catalog = nil
	a.status.LLM.Alias, a.status.LLM.ModelID = "Whatever", "from-server"
	if id, _, _ := a.servedModel(); id != "from-server" {
		t.Errorf("server id without a catalog: %q", id)
	}
}

func TestUnknownModelRunningIsShownByAlias(t *testing.T) {
	a := fakeApp(t)
	a.status.LLM.Alias, a.status.LLM.ModelID = "Totally-Other-Model", ""
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "Totally-Other-Model") {
		t.Errorf("the running model's alias should be the headline; texts %q", tt.Texts())
	}
	running := 0
	for _, s := range tt.Texts() {
		if s == "运行中" {
			running++
		}
	}
	if running != 1 { // the pill in the "当前模型" card; a rail row marked running would add one
		t.Errorf("no rail row may be marked running for a model it does not list: %d pills", running)
	}
}

func TestCustomModelAppearsInRailAndSwitchMenu(t *testing.T) {
	a := fakeApp(t)
	a.catalog = append(a.catalog, Model{ID: "my-70b", Name: "My-70B-Instruct-Q5_K_M", Alias: "My-70B-Instruct-Q5_K_M", SizeGB: 41.5})
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "My-70B-Instruct-Q5_K_M") {
		t.Fatalf("the user's own model should be in the list; texts %q", tt.Texts())
	}
	if err := tt.Click("切换到 My-70B-Instruct-Q5_K_M"); err != nil {
		t.Fatal(err)
	}
	if a.switching != "" || !hasTextPart(tt, "确认切换") {
		t.Fatalf("switch must ask first for the custom model: switching=%q texts %q", a.switching, tt.Texts())
	}
	if err := tt.Click("确认切换"); err != nil {
		t.Fatal(err)
	}
	if a.switching != "my-70b" {
		t.Errorf("confirming must load the custom model: switching=%q", a.switching)
	}
}

func TestNoRunnersShowsTheExportHintAndNoModels(t *testing.T) {
	a := fakeApp(t)
	a.catalog = nil
	a.status.LLM.Online = false
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "colab-gpu-llm-service") || !hasTextPart(tt, "colab_kernel_runners.json") {
		t.Errorf("the empty list should say where the runners come from; texts %q", tt.Texts())
	}
	if hasTextPart(tt, "Gemma-4-31B (ARA Heretic Q8_0)") {
		t.Error("no model names may be invented when there is no runners file")
	}
	fresh := newApp(Paths{ConfigDir: t.TempDir(), Settings: filepath.Join(t.TempDir(), "s.json")})
	if len(fresh.catalog) != 0 || fresh.tsURL != "" {
		t.Errorf("an install without runners starts empty: %+v %q", fresh.catalog, fresh.tsURL)
	}
}

// ─── control token ───────────────────────────────────────────────────────────

func TestPostCarriesTheBearerFromTheTokenFile(t *testing.T) {
	var gotAuth atomic.Value
	var gets atomic.Int32
	hs := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth.Store(r.Header.Get("Authorization"))
		if r.Method == "GET" {
			gets.Add(1)
			if r.Header.Get("Authorization") != "" {
				t.Error("status polls need no token and must not send it")
			}
		}
		_, _ = w.Write([]byte(`{"server_version":"x"}`))
	}))
	defer hs.Close()
	tok := filepath.Join(t.TempDir(), "server_token.json")
	s := newServer(tok)

	if _, err := s.Post(hs.URL, "/api/touch", map[string]any{}); err != nil || gotAuth.Load() != "" {
		t.Fatalf("no token file yet (an older server): err %v header %q", err, gotAuth.Load())
	}
	_ = os.WriteFile(tok, []byte(`{"token":"abc123","saved":1}`), 0o644)
	if _, err := s.Post(hs.URL, "/api/touch", map[string]any{}); err != nil || gotAuth.Load() != "Bearer abc123" {
		t.Fatalf("err %v header %q", err, gotAuth.Load())
	}
	// A wake or discover rewrites the file while the console runs: the next call uses it.
	_ = os.WriteFile(tok, []byte(`{"token":"def456"}`), 0o644)
	_, _ = s.Post(hs.URL, "/api/config_idle", map[string]any{})
	if gotAuth.Load() != "Bearer def456" {
		t.Errorf("token not re-read: %q", gotAuth.Load())
	}
	if _, err := s.Status(hs.URL, ""); err != nil || gets.Load() != 1 {
		t.Errorf("status: %v", err)
	}
	for _, bad := range []string{`not json`, `{"token":7}`, `{}`} {
		_ = os.WriteFile(tok, []byte(bad), 0o644)
		if s.token() != "" {
			t.Errorf("%q should read as no token", bad)
		}
	}
	if newServer("").token() != "" {
		t.Error("no path, no token")
	}
}

func TestAnUnauthorizedAnswerIsDistinguishedAndTriggersARefresh(t *testing.T) {
	hs := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fresh" {
			w.WriteHeader(401)
			return
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer hs.Close()
	tok := filepath.Join(t.TempDir(), "server_token.json")
	_ = os.WriteFile(tok, []byte(`{"token":"stale"}`), 0o644)
	s := newServer(tok)
	_, err := s.Post(hs.URL, "/api/disconnect", nil)
	if err != errUnauthorized {
		t.Fatalf("401 must be errUnauthorized, got %v", err)
	}
	if !strings.Contains(shortErr(err), "令牌") {
		t.Errorf("the message should name the token: %q", shortErr(err))
	}
	a := fakeApp(t)
	a.noteAuth(err)
	if !a.tokenStale || !a.discovering {
		t.Errorf("a refused call must start fetching the current token: stale %v discovering %v", a.tokenStale, a.discovering)
	}
	a.discovering = false
	a.noteAuth(fmt.Errorf("HTTP 500"))
	if a.discovering {
		t.Error("only a 401 refreshes the token")
	}
	_ = os.WriteFile(tok, []byte(`{"token":"fresh"}`), 0o644)
	if _, err := s.Post(hs.URL, "/api/disconnect", nil); err != nil {
		t.Errorf("after the refresh the call goes through: %v", err)
	}
}

func TestBridgeTokenPathIsPerUserNotInTheRepo(t *testing.T) {
	local := t.TempDir()
	p := pathsFor(local, filepath.Join(local, "Programs", "Colab-yoga"))
	if p.Token != filepath.Join(local, "Colab-yoga", "server_token.json") {
		t.Errorf("token path: %q", p.Token)
	}
}
