package main

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/egoist/mygo/ui"
)

// settlements builds n observed settlements every gap from start, each charging rate*gap,
// with jitter added to alternate ones (Colab's steps are not even).
func settlements(start time.Time, startBal float64, n int, gap time.Duration, rate, jitter float64) []LedgerPoint {
	pts := make([]LedgerPoint, 0, n)
	bal := startBal
	for i := 1; i <= n; i++ {
		step := rate * gap.Hours()
		if i%2 == 0 {
			step += jitter
		} else {
			step -= jitter
		}
		bal -= step
		pts = append(pts, LedgerPoint{At: start.Add(time.Duration(i) * gap), Balance: bal})
	}
	return pts
}

func TestReadLedger(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.json")
	body := `{"balance": 98.76, "checked_ts": 1700000600.5, "active": 1, "official_rate": 10.05, "poll_s": 15,
		"idle_balance": 100.0, "idle_ts": 1700000000,
		"session": {"start_balance": 100.0, "start_ts": 1700000060, "exact": true, "endpoint": "vm1",
			"points": [[1700000300, 99.6], [1700000600, 98.76]], "official_rate": 10.05}}`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	l := readLedger(path)
	if l.Balance != 98.76 || l.Active != 1 || l.OfficialRate != 10.05 || l.PollEvery != 15*time.Second {
		t.Fatalf("ledger %+v", l)
	}
	if !l.CheckedAt.Equal(time.Unix(1700000600, 500_000_000)) {
		t.Errorf("checked at %v", l.CheckedAt)
	}
	s := l.Session
	if s == nil || !s.Exact || s.Ended || s.StartBalance != 100 || len(s.Points) != 2 || s.Points[1].Balance != 98.76 {
		t.Fatalf("session %+v", s)
	}
	if math.Abs(l.Spent()-1.24) > 1e-9 || !l.LastSettle().Equal(time.Unix(1700000600, 0)) {
		t.Errorf("spent %v last %v", l.Spent(), l.LastSettle())
	}
	if got := readLedger(filepath.Join(t.TempDir(), "missing.json")); got.Session != nil || !got.CheckedAt.IsZero() {
		t.Errorf("missing file gave %+v", got)
	}
}

func TestMeasuredRateNeedsThreeSettlementsAndFollowsTheWindow(t *testing.T) {
	t0 := time.Unix(1_700_000_000, 0)
	l := Ledger{Session: &LedgerSession{StartBalance: 500}}
	l.Session.Points = settlements(t0, 500, 3, 5*time.Minute, 10.05, 0.02)
	if _, _, ok := l.MeasuredRate(); ok {
		t.Fatal("two steps are too few to measure")
	}
	l.Session.Points = settlements(t0, 500, 4, 5*time.Minute, 10.05, 0.02)
	rate, steps, ok := l.MeasuredRate()
	if !ok || steps != 3 || math.Abs(rate-10.05) > 0.3 {
		t.Fatalf("rate %v steps %d ok %v", rate, steps, ok)
	}
	// A single jittered step would read 10.45 CU/h; across the window it averages out.
	l.Session.Points = settlements(t0, 500, 12, 5*time.Minute, 10.05, 0.03)
	rate, steps, _ = l.MeasuredRate()
	if steps != 7 || math.Abs(rate-10.05) > 0.1 {
		t.Errorf("rate %v over %d steps", rate, steps)
	}
	// The rate changes (a second GPU): within the window the new rate is what shows.
	first := settlements(t0, 500, 6, 5*time.Minute, 10.05, 0)
	last := first[len(first)-1]
	later := settlements(last.At, last.Balance, 8, 5*time.Minute, 20.10, 0)
	l.Session.Points = append(first, later...)
	if rate, _, _ = l.MeasuredRate(); math.Abs(rate-20.10) > 0.01 {
		t.Errorf("after the change the rate is %v", rate)
	}
}

func TestLedgerLiveness(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	l := Ledger{Active: 1, CheckedAt: now.Add(-20 * time.Second), Session: &LedgerSession{}}
	if !l.Live(now) {
		t.Error("a fresh reading of a running session is live")
	}
	if l.Live(now.Add(3 * time.Minute)) {
		t.Error("a ledger nobody has read for minutes is not live")
	}
	l.Session.Ended = true
	if l.Live(now) {
		t.Error("an ended session is not live")
	}
	a := fakeApp(t)
	a.now = func() time.Time { return now }
	a.ledger = Ledger{Active: 1, CheckedAt: now.Add(-20 * time.Second), Session: &LedgerSession{}}
	a.acct.Active, a.acctAt = 0, now.Add(-5*time.Second)
	if a.ledgerLive(now) {
		t.Error("a newer account reading with nothing assigned overrides the ledger")
	}
}

// billingApp is fakeApp with the keeper's ledger: started from 2,800.00 CU, five settlements
// at 10.05 CU/h, the last one two minutes ago.
func billingApp(t *testing.T, exact bool) (*App, time.Time) {
	a := fakeApp(t)
	now := time.Now()
	a.now = func() time.Time { return now }
	t0 := now.Add(-27 * time.Minute)
	pts := settlements(t0, 2800, 5, 5*time.Minute, 10.05, 0.01)
	a.ledger = Ledger{Balance: pts[len(pts)-1].Balance, CheckedAt: now.Add(-10 * time.Second), Active: 1,
		OfficialRate: 10.05, Session: &LedgerSession{StartBalance: 2800, StartAt: t0, Exact: exact, Points: pts}}
	a.acctAt = now.Add(-30 * time.Second)
	return a, now
}

func TestBillShowsWhatTheLedgerDeducted(t *testing.T) {
	a, _ := billingApp(t, true)
	spent := a.ledger.Spent()
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	rate, _, _ := a.ledger.MeasuredRate()
	for _, want := range []string{
		thousands(a.ledger.Balance) + " CU", "上次结算 2 分钟前",
		fmt.Sprintf("%.2f CU/h", rate), "账本实测",
		fmt.Sprintf("%.2f CU", spent), "账本实扣",
		"以 Colab 官方账本的实际扣减为准",
	} {
		if !hasTextPart(tt, want) {
			t.Errorf("bill lacks %q; texts %q", want, tt.Texts())
		}
	}
	if math.Abs(spent-5*10.05/12) > 0.05 {
		t.Errorf("spent %v", spent)
	}
	if hasTextPart(tt, "估算") || hasTextPart(tt, "官方报价") {
		t.Errorf("an estimate shows next to the ledger: %q", tt.Texts())
	}
}

func TestPendingIsWhatRanSinceTheLastSettlement(t *testing.T) {
	a, now := billingApp(t, true)
	rate, _, _ := a.ledger.MeasuredRate()
	want := rate * 2 / 60 // the last settlement was 2 minutes ago
	if got := a.pendingCU(now); math.Abs(got-want) > 1e-9 {
		t.Errorf("pending %v, want %v", got, want)
	}
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if s := fmt.Sprintf("约 %.2f 待结算", want); !hasTextPart(tt, s) {
		t.Errorf("bill lacks %q; texts %q", s, tt.Texts())
	}
	// Before the first settlement it counts from the session start: not a bare 0.00.
	a.ledger.Session.Points = nil
	a.ledger.Session.StartAt = now.Add(-4 * time.Minute)
	a.ledger.Balance = a.ledger.Session.StartBalance
	tt = ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if s := fmt.Sprintf("约 %.2f 待结算", 10.05*4/60); !hasTextPart(tt, "0.00 CU") || !hasTextPart(tt, s) {
		t.Errorf("first minutes lack %q; texts %q", s, tt.Texts())
	}
	// Settlements that stop arriving do not grow it past the cap.
	a.ledger.Session.StartAt = now.Add(-90 * time.Minute)
	if got := a.pendingCU(now); math.Abs(got-10.05*ledgerPendingCap.Hours()) > 1e-9 {
		t.Errorf("capped pending %v", got)
	}
	a.ledger.Active = 0
	if got := a.pendingCU(now); got != 0 {
		t.Errorf("nothing is pending without a live session: %v", got)
	}
}

func TestBillMarksASessionWhoseStartWasNotSeen(t *testing.T) {
	a, _ := billingApp(t, false)
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	for _, want := range []string{fmt.Sprintf("≥ %.2f CU", a.ledger.Spent()),
		"自 " + a.ledger.Session.StartAt.Format("15:04") + " 起实扣"} {
		if !hasTextPart(tt, want) {
			t.Errorf("bill lacks %q; texts %q", want, tt.Texts())
		}
	}
}

func TestBillBeforeTheLedgerHasEnough(t *testing.T) {
	a, now := billingApp(t, true)
	a.ledger.Session.Points = a.ledger.Session.Points[:2]
	tt := ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	if !hasTextPart(tt, "10.05 CU/h") || !hasTextPart(tt, "官方报价") {
		t.Errorf("with two settlements the advertised rate shows: %q", tt.Texts())
	}
	a.ledger = Ledger{}
	a.acctAt = now
	tt = ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true})
	for _, want := range []string{"≈ 13.4 CU", "估算"} {
		if !hasTextPart(tt, want) {
			t.Errorf("without a ledger the bill lacks %q; texts %q", want, tt.Texts())
		}
	}
}

func TestCountdownAndUptimeRunBetweenPolls(t *testing.T) {
	a := fakeApp(t)
	clock := time.Now()
	a.now = func() time.Time { return clock }
	a.applyStatus(a.status) // IdleRemaining 1560, Uptime 4790, anchored now
	texts := func() string {
		return strings.Join(ui.NewTesterWith(a.view, 1440, 900, ui.TesterOptions{Dark: true}).Texts(), "\n")
	}
	if s := texts(); !strings.Contains(s, "闲置 26 分 00 秒") || !strings.Contains(s, "01:19:50") {
		t.Fatalf("at the poll: %s", s)
	}
	clock = clock.Add(7 * time.Second)
	if s := texts(); !strings.Contains(s, "闲置 25 分 53 秒") || !strings.Contains(s, "01:19:57") {
		t.Errorf("7 s later: %s", s)
	}
	clock = clock.Add(10 * time.Minute) // polls stopped: it runs on for at most a minute
	if s := texts(); !strings.Contains(s, "闲置 25 分 00 秒") {
		t.Errorf("a stalled poll must not run the countdown down: %s", s)
	}
	a.applyStatus(a.status)
	if s := texts(); !strings.Contains(s, "闲置 26 分 00 秒") {
		t.Errorf("the next poll re-anchors it: %s", s)
	}
}
