package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/egoist/mygo/ui"
)

func TestNewerThanComparesPlainVersionsOnly(t *testing.T) {
	for _, c := range []struct {
		tag, current string
		want         bool
	}{
		{"v0.5.4", "0.5.3", true},
		{"v0.6.0", "0.5.9", true},
		{"v1.0.0", "0.99.99", true},
		{"v0.5.10", "0.5.9", true}, // numbers, not text
		{"v0.5.3", "0.5.3", false},
		{"v0.5.2", "0.5.3", false},
		{"v0.6.0-beta.1", "0.5.3", false}, // a prerelease is never announced
		{"v0.6.0", "", false},             // a development build has no version to compare
		{"", "0.5.3", false},
		{"latest", "0.5.3", false},
	} {
		if got := newerThan(c.tag, c.current); got != c.want {
			t.Errorf("newerThan(%q, %q) = %v", c.tag, c.current, got)
		}
	}
}

// serveLatest points latestReleaseURL at a server that answers the way GitHub does.
func serveLatest(t *testing.T, location string, hits *int) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*hits++
		if location == "" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Location", location)
		w.WriteHeader(http.StatusFound)
	}))
	old := latestReleaseURL
	latestReleaseURL = srv.URL
	t.Cleanup(func() { latestReleaseURL = old; srv.Close() })
}

func TestLatestTagReadsTheRedirectWithoutFollowingIt(t *testing.T) {
	hits := 0
	serveLatest(t, "https://github.com/incredible404-rgb/Colab-yoga/releases/tag/v0.9.0", &hits)
	tag, err := latestTag(context.Background())
	if err != nil || tag != "v0.9.0" || hits != 1 {
		t.Errorf("tag %q err %v hits %d", tag, err, hits)
	}
}

func TestLatestTagIsEmptyWhenThereIsNoRelease(t *testing.T) {
	hits := 0
	serveLatest(t, "", &hits)
	if tag, err := latestTag(context.Background()); err != nil || tag != "" {
		t.Errorf("tag %q err %v", tag, err)
	}
}

func TestACheckRemembersTheAnswerAndIsNotRepeatedForADay(t *testing.T) {
	hits := 0
	serveLatest(t, "https://github.com/incredible404-rgb/Colab-yoga/releases/tag/v0.9.0", &hits)
	a := fakeApp(t)
	a.setVersion("0.5.3")
	if !a.updateDue() {
		t.Fatal("a build that never asked is due")
	}
	a.checkUpdate()
	if a.updateTag != "v0.9.0" || a.updateDue() {
		t.Errorf("after a check: tag %q due %v", a.updateTag, a.updateDue())
	}
	// The next launch shows the row from settings, before any request.
	b := newApp(a.paths)
	b.now = a.now
	b.setVersion("0.5.3")
	if b.updateTag != "v0.9.0" || b.updateDue() || hits != 1 {
		t.Errorf("next launch: tag %q due %v hits %d", b.updateTag, b.updateDue(), hits)
	}
	later := a.now().Add(25 * time.Hour)
	b.now = func() time.Time { return later }
	if !b.updateDue() {
		t.Error("a day later the check is due again")
	}
	// Once this build is the announced release, the remembered answer no longer shows.
	c := newApp(a.paths)
	c.setVersion("0.9.0")
	if c.updateTag != "" {
		t.Errorf("a current build announces nothing: %q", c.updateTag)
	}
}

func TestUpdateCheckOffAsksNothingAndShowsNothing(t *testing.T) {
	a := fakeApp(t)
	_ = a.set.Set(map[string]any{"updateCheck": "off", "updateLatest": "v0.9.0"})
	a.setVersion("0.5.3")
	if a.updateDue() || a.updateTag != "" {
		t.Errorf("off: due %v tag %q", a.updateDue(), a.updateTag)
	}
	dev := fakeApp(t)
	dev.setVersion("")
	if dev.updateDue() {
		t.Error("a development build does not ask")
	}
}

func TestTheUpdateRowOpensTheReleasePage(t *testing.T) {
	a := fakeApp(t)
	tt := ui.NewTesterWith(a.view, 1024, 640, ui.TesterOptions{Dark: true})
	tt.Frame()
	if err := tt.Click("新版本 0.9.0，查看更新说明"); err == nil {
		t.Fatal("no row while the build is current")
	}
	a.updateTag = "v0.9.0"
	tt.Frame()
	if err := tt.Click("新版本 0.9.0，查看更新说明"); err != nil {
		t.Fatal(err)
	}
	want := "https://github.com/incredible404-rgb/Colab-yoga/releases/tag/v0.9.0"
	if len(a.opened) != 1 || a.opened[0] != want {
		t.Errorf("opened %v", a.opened)
	}
}
