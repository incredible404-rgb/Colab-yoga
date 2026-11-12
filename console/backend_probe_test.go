package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The tailnet probe asks /v1/models and /slots together once /health answers: with each
// endpoint taking 300 ms, a probe takes about two round trips, not three.
func TestProbeLlamaAsksModelsAndSlotsTogether(t *testing.T) {
	const lag = 300 * time.Millisecond
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(lag)
		switch r.URL.Path {
		case "/health":
			_, _ = w.Write([]byte(`{"status":"ok"}`))
		case "/v1/models":
			_, _ = w.Write([]byte(`{"data":[{"id":"Gemma","meta":{"n_ctx":262144}}]}`))
		case "/slots":
			_, _ = w.Write([]byte(`[{"is_processing":true},{"is_processing":false}]`))
		}
	}))
	defer srv.Close()
	start := time.Now()
	p := probeLlama(srv.URL)
	took := time.Since(start)
	if !p.OK || p.Alias != "Gemma" || p.NCtx != 262144 || p.SlotsTotal != 2 || p.SlotsBusy != 1 {
		t.Fatalf("probe %+v", p)
	}
	if took >= 3*lag-50*time.Millisecond {
		t.Errorf("probe took %v: models and slots should be asked together", took)
	}
}

// The local MCP counts as up only when /health identifies it. A listening port proves
// nothing, so a foreign HTTP listener on the same port must not turn the card green.
func TestProbeMCPNeedsTheServiceToIdentifyItself(t *testing.T) {
	serve := func(status int, body string) string {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/health" {
				http.NotFound(w, r)
				return
			}
			w.WriteHeader(status)
			_, _ = w.Write([]byte(body))
		}))
		t.Cleanup(srv.Close)
		return srv.URL
	}

	for _, c := range []struct {
		label, body string
	}{
		{"server field", `{"ok":true,"server":"local_mcp"}`},
		{"older name field", `{"ok":true,"name":"local_mcp"}`},
	} {
		up, name, errText := probeMCP(serve(200, c.body))
		if !up || name != "local_mcp" || errText != "" {
			t.Errorf("%s: up=%v name=%q err=%q", c.label, up, name, errText)
		}
	}

	for _, c := range []struct {
		label, body string
		status      int
		wantErr     string
	}{
		{"foreign service", `{"ok":true,"server":"ollama"}`, 200, "其他服务"},
		{"foreign html listener", `<html>hi</html>`, 200, "没报出服务名"},
		{"no identity", `{"ok":true}`, 200, "没报出服务名"},
		{"not ready", `{"ok":false,"server":"local_mcp"}`, 200, "未就绪"},
		{"old version without /health", `not found`, 404, "旧版本机 MCP"},
	} {
		up, _, errText := probeMCP(serve(c.status, c.body))
		if up {
			t.Errorf("%s: counted as the local MCP", c.label)
		}
		if !strings.Contains(errText, c.wantErr) {
			t.Errorf("%s: err %q lacks %q", c.label, errText, c.wantErr)
		}
	}
}

func TestProbeMCPWithoutAListenerIsDown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // nothing listens there now
	if up, _, errText := probeMCP(url); up || errText == "" {
		t.Errorf("a closed port: up=%v err=%q", up, errText)
	}
}

func TestStatCacheRereadsOnlyWhenTheFileChanges(t *testing.T) {
	path := filepath.Join(t.TempDir(), "providers.json")
	_ = os.WriteFile(path, []byte(`{"a":1}`), 0o644)
	var c statCache
	b1, _ := c.read(path)
	b2, _ := c.read(path)
	if string(b1) != `{"a":1}` || &b1[0] != &b2[0] {
		t.Fatal("an unchanged file should come from the cache")
	}
	_ = os.WriteFile(path, []byte(`{"a":22}`), 0o644)
	later := time.Now().Add(time.Second)
	_ = os.Chtimes(path, later, later)
	if b3, _ := c.read(path); string(b3) != `{"a":22}` {
		t.Errorf("changed file not reread: %s", b3)
	}
}
