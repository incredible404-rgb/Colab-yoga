package main

// The model list is whatever colab_kernel_runners.json holds (exported by
// colab-gpu-llm-service's colab_kernel.py): one entry per model, keyed by its slug, with the
// `alias` llama-server reports and the shard `files` whose sizes add up to the download.
// An entry may carry a display `name`, `tags` and a `note`; without them the model shows
// its alias and the summed size. The console knows no model of its own.

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
)

// Model is a colab_kernel LLM the console can run on :8081.
type Model struct {
	ID, Name, Alias string
	SizeGB          float64
	Tags            []string
	Note            string
}

// Runner is what the console reads from one runners entry. The entry's script (`code`,
// which holds API keys) is only searched for the Tailscale service host, never kept.
type Runner struct {
	ID, Alias, Title string
	Name, Note       string
	Tags             []string
	SizeBytes        float64
	FQDN             string
}

var (
	serviceFQDNRe = regexp.MustCompile(`SERVICE_FQDN\s*=\s*["']([A-Za-z0-9.-]+\.ts\.net)["']`)
	anyTsNetRe    = regexp.MustCompile(`\b((?:[a-z0-9-]+\.)+ts\.net)\b`)
)

// fqdnIn is the Tailscale Service host in a runner script: SERVICE_FQDN when the script
// names it, else the first *.ts.net host in the text.
func fqdnIn(code string) string {
	if m := serviceFQDNRe.FindStringSubmatch(code); m != nil {
		return m[1]
	}
	if m := anyTsNetRe.FindStringSubmatch(code); m != nil {
		return m[1]
	}
	return ""
}

// readRunners parses the runners file in file order. An entry that is not an object is
// skipped; a file that is not a JSON object is an error.
func readRunners(path string) ([]Runner, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	dec := json.NewDecoder(f)
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return nil, fmt.Errorf("runners file is not a JSON object")
	}
	var out []Runner
	for dec.More() {
		keyTok, err := dec.Token()
		if err != nil {
			return out, err
		}
		key, _ := keyTok.(string)
		var raw json.RawMessage
		if err := dec.Decode(&raw); err != nil {
			if err == io.EOF {
				break
			}
			return out, err
		}
		var e struct {
			Slug  string   `json:"slug"`
			Alias string   `json:"alias"`
			Title string   `json:"title"`
			Name  string   `json:"name"`
			Note  string   `json:"note"`
			Tags  []string `json:"tags"`
			Code  string   `json:"code"`
			Files []struct {
				Size float64 `json:"size"`
			} `json:"files"`
		}
		if json.Unmarshal(raw, &e) != nil {
			continue
		}
		r := Runner{ID: strings.TrimSpace(e.Slug), Alias: strings.TrimSpace(e.Alias), Title: e.Title,
			Name: strings.TrimSpace(e.Name), Note: e.Note, Tags: e.Tags, FQDN: fqdnIn(e.Code)}
		if r.ID == "" {
			r.ID = strings.TrimSpace(key)
		}
		if r.ID == "" {
			continue
		}
		for _, file := range e.Files {
			r.SizeBytes += file.Size
		}
		out = append(out, r)
	}
	return out, nil
}

// catalogFrom is the model list for these runners: an entry's own name, tags and note when
// it has them, else its alias; the size is summed from its files.
func catalogFrom(runners []Runner) []Model {
	var out []Model
	for _, r := range runners {
		m := Model{ID: r.ID, Name: r.Name, Alias: r.Alias, SizeGB: r.SizeBytes / 1e9, Tags: r.Tags, Note: r.Note}
		if m.Name == "" {
			m.Name = r.Alias
		}
		if m.Name == "" {
			m.Name = r.ID
		}
		out = append(out, m)
	}
	return out
}

// tailnetURLFrom is the access address the runners publish, from the first runner that
// names a service host; "" when none does.
func tailnetURLFrom(runners []Runner) string {
	for _, r := range runners {
		if r.FQDN != "" {
			return "http://" + r.FQDN + ":443"
		}
	}
	return ""
}

// defaultTailscaleURL finds the Tailscale Service host the colab_kernel runners publish:
// the SERVICE_FQDN the scripts name, else any *.ts.net host in them.
func defaultTailscaleURL(p Paths) string {
	path := p.runnersFile()
	if path == "" {
		return ""
	}
	runners, _ := readRunners(path)
	return tailnetURLFrom(runners)
}

// loadCatalog reads the runners file when it changed since `last` (same path, size and
// modification time means unchanged). found is false when there is no file or it holds no
// model, which the views turn into the "export the runners" hint.
func loadCatalog(p Paths, last runnersSig) (models []Model, tsURL string, sig runnersSig, changed bool) {
	path := p.runnersFile()
	if path == "" {
		return nil, "", runnersSig{}, last != runnersSig{}
	}
	fi, err := os.Stat(path)
	if err != nil {
		return nil, "", runnersSig{}, last != runnersSig{}
	}
	sig = runnersSig{path, fi.Size(), fi.ModTime().UnixNano()}
	if sig == last {
		return nil, "", sig, false
	}
	runners, _ := readRunners(path)
	return catalogFrom(runners), tailnetURLFrom(runners), sig, true
}

type runnersSig struct {
	path    string
	size    int64
	modNano int64
}

// ─── Lookups on the App's current catalog ────────────────────────────────────

func (a *App) modelByID(id string) *Model {
	for i := range a.catalog {
		if a.catalog[i].ID == id && id != "" {
			return &a.catalog[i]
		}
	}
	return nil
}

func (a *App) modelByAlias(alias string) *Model {
	for i := range a.catalog {
		if a.catalog[i].Alias == alias && alias != "" {
			return &a.catalog[i]
		}
	}
	return nil
}

func (a *App) modelName(id string) string {
	if m := a.modelByID(id); m != nil {
		return m.Name
	}
	return id
}

// reloadCatalog refreshes the list from the runners file if it changed. An access address
// found there fills an empty one (never replaces what the user typed).
func (a *App) reloadCatalog() {
	models, ts, sig, changed := loadCatalog(a.paths, a.catalogSig)
	if !changed {
		return
	}
	a.catalog, a.catalogSig = models, sig
	if a.tsURL == "" && ts != "" {
		a.tsURL = ts
	}
}

// noModelsHint is shown where the model list would be when there is none.
const noModelsHint = "没有找到模型清单 colab_kernel_runners.json。在 colab-gpu-llm-service 里运行 python -X utf8 scripts/colab_kernel.py --all，会自动写入 %LOCALAPPDATA%\\Colab-yoga\\colab_kernel_runners.json（macOS 为 ~/Library/Application Support/Colab-yoga/colab_kernel_runners.json；免安装包解压到任意目录都能自动识别，也可以放到 Colab-yoga.exe / Colab-yoga.app 旁边）。"
