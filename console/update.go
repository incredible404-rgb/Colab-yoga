package main

// A new release is announced, never installed: the console asks GitHub once a day which
// release is the latest and, when it is newer than this build, shows one row that opens the
// release page. Downloading stays the user's own act, with the same trust as their first
// download. "updateCheck": "off" in settings.json stops the request altogether.

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	releasesPage  = "https://github.com/incredible404-rgb/Colab-yoga/releases"
	updateEvery   = 24 * time.Hour
	updateTimeout = 8 * time.Second
)

// latestReleaseURL is asked without following its redirect: GitHub answers with the page of
// the latest release, whose last path segment is the tag. No API, so no rate limit and no JSON.
var latestReleaseURL = releasesPage + "/latest"

// latestTag is the tag of the latest published release ("v0.5.3"), "" when GitHub does not
// name one (no release yet, or an answer that is not the redirect).
func latestTag(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, latestReleaseURL, nil)
	if err != nil {
		return "", err
	}
	client := &http.Client{Timeout: updateTimeout, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	res, err := client.Do(req)
	if err != nil {
		return "", err
	}
	res.Body.Close()
	loc := res.Header.Get("Location")
	i := strings.LastIndex(loc, "/releases/tag/")
	if i < 0 {
		return "", nil
	}
	return strings.TrimSpace(loc[i+len("/releases/tag/"):]), nil
}

// versionNumbers is "v1.2.3" or "1.2.3" as [1 2 3]; nil for anything else, a prerelease
// ("1.2.3-beta.1") included, so that only plain releases are ever compared.
func versionNumbers(v string) []int {
	parts := strings.Split(strings.TrimPrefix(strings.TrimSpace(v), "v"), ".")
	if len(parts) != 3 {
		return nil
	}
	out := make([]int, 3)
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 {
			return nil
		}
		out[i] = n
	}
	return out
}

// newerThan reports whether release tag is a later version than current. Either one that
// is not a plain x.y.z (a development build, a prerelease tag) compares as not newer.
func newerThan(tag, current string) bool {
	a, b := versionNumbers(tag), versionNumbers(current)
	if a == nil || b == nil {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return a[i] > b[i]
		}
	}
	return false
}

// loadUpdate reads what the last check found, so the row shows at launch without a request.
func (a *App) loadUpdate(m map[string]any) {
	a.updateOff = str(m, "updateCheck", "") == "off"
	a.updateAt = time.Unix(int64(num(m, "updateCheckedAt", 0)), 0)
	if tag := str(m, "updateLatest", ""); !a.updateOff && newerThan(tag, a.version) {
		a.updateTag = tag
	}
}

// updateDue reports whether a check should run now.
func (a *App) updateDue() bool {
	return !a.updateOff && versionNumbers(a.version) != nil && a.now().Sub(a.updateAt) >= updateEvery
}

// checkUpdate asks for the latest release and remembers the answer and when it was asked.
// A failed request is tried again at the next launch or the next day, whichever comes first.
func (a *App) checkUpdate() {
	ctx, cancel := context.WithTimeout(context.Background(), updateTimeout)
	defer cancel()
	tag, err := latestTag(ctx)
	a.apply(func() {
		a.updateAt = a.now()
		if err != nil {
			return
		}
		a.updateTag = ""
		if newerThan(tag, a.version) {
			a.updateTag = tag
		}
		_ = a.set.Set(map[string]any{"updateCheckedAt": a.updateAt.Unix(), "updateLatest": tag})
	})
}

// openUpdate opens the page of the release the row announces.
func (a *App) openUpdate() {
	if a.updateTag != "" {
		a.openURL(releasesPage + "/tag/" + a.updateTag)
	}
}
