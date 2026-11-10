package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
