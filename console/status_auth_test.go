package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

func TestStatusCarriesTheCurrentControlToken(t *testing.T) {
	var expected atomic.Value
	expected.Store("Bearer first-token")
	hs := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != expected.Load().(string) {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`{"console_lines":["private log"]}`))
	}))
	defer hs.Close()
	tokenPath := filepath.Join(t.TempDir(), "server_token.json")
	server := newServer(tokenPath)
	if _, err := server.Status(hs.URL, ""); err != errUnauthorized {
		t.Fatalf("missing token must fail closed: %v", err)
	}
	for _, token := range []string{"first-token", "rotated-token"} {
		expected.Store("Bearer " + token)
		if err := os.WriteFile(tokenPath, []byte(`{"token":"`+token+`"}`), 0o600); err != nil {
			t.Fatal(err)
		}
		status, err := server.Status(hs.URL, "")
		if err != nil || len(status.Console) != 1 {
			t.Fatalf("authenticated status after token update: status=%v error=%v", status, err)
		}
	}
}

func TestAccountRefreshFailurePreservesBillingAndOffersRebindingWhenRequired(t *testing.T) {
	for _, reauth := range []bool{false, true} {
		a := fakeApp(t)
		before := a.acct
		a.connected = false
		if !reauth {
			a.acct = Account{}
		}
		a.applyAccountResult(map[string]any{"ok": false, "error": "verification unavailable", "reauth_required": reauth})
		if reauth {
			if !a.signingIn() {
				t.Fatal("unusable saved credentials must offer sign-in")
			}
			if a.acct.Active != before.Active || a.acct.Balance != before.Balance {
				t.Fatal("offering a new binding must preserve the last known billing state")
			}
		} else if a.signingIn() || a.acct.Known {
			t.Fatal("temporary verification failure must not force sign-in")
		}
	}
	a := fakeApp(t)
	before := a.acct
	a.applyAccountResult(map[string]any{"ok": false, "error": "network unavailable", "retryable": true})
	if a.acct != before {
		t.Fatal("temporary verification failure must preserve the whole known account")
	}
}

func TestStatusPollRecoversAStaleControlToken(t *testing.T) {
	hs := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer hs.Close()
	a := fakeApp(t)
	a.serverURL = hs.URL
	a.pollStatus(hs.URL)
	if !a.tokenStale || !a.discovering {
		t.Fatalf("status 401 must trigger token discovery: stale=%v discovering=%v", a.tokenStale, a.discovering)
	}
}
