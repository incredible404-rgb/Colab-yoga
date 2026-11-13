// Command console is Colab-yoga, a management console for a Colab GPU that serves the
// owner's LLMs to the web UI, Magpie and MCP: what it costs, which model runs, and
// whether each way in works. It lives in the tray; the window is opened on demand.
package main

import (
	_ "embed"
	"log"
	"os"
	"runtime/debug"
	"time"

	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
)

//go:embed resources/tray.png
var trayIcon []byte

func userHome() (string, error) { return os.UserHomeDir() }

func main() {
	// A second launch (desktop shortcut, Start menu) shows the running window instead.
	if !mygo.App.RequestSingleInstanceLock() {
		return
	}
	app := newApp(findPaths())
	app.live = true
	writeInstallAnchor(app.paths) // only the real start: tests build Paths of their own
	mygo.App.OnSecondInstance(func([]string, string) { app.showWindow() })
	mygo.App.OnActivate(func(bool) { app.showWindow() })
	// Registering a listener keeps the app running in the tray when the window closes.
	mygo.App.OnWindowAllClosed(func() {})
	mygo.App.WhenReady(func() {
		app.makeTray()
		app.showWindow()
		go app.run()
	})
	if err := mygo.App.Run(); err != nil {
		log.Fatal(err)
	}
}

func (a *App) makeTray() {
	item := func(label string, fn func()) *mygo.MenuItem {
		return &mygo.MenuItem{Label: label, Click: func(*mygo.MenuItem, *mygo.Window) { fn() }}
	}
	menu := mygo.NewMenu([]*mygo.MenuItem{
		item("打开 Colab-yoga", a.showWindow),
		item("打开网页对话", a.openWeb),
		item("续时", a.touch),
		item("释放全部实例…", func() {
			// Never release from the tray directly: show the window and its confirmation.
			a.showWindow()
			a.page, a.askRelease = "dashboard", true
		}),
		mygo.Separator(),
		item("退出（GPU 由后台保活照常管理）", func() { mygo.App.Quit() }),
	})
	t, err := mygo.NewTray(mygo.TrayOptions{ID: "colab-yoga", Icon: trayIcon, ToolTip: "Colab-yoga", Menu: menu})
	if err != nil {
		log.Printf("tray: %v", err)
		return
	}
	a.tray = t
	// Windows keeps an icon after its owner exits unless it is removed while
	// the native event loop is still running.
	mygo.App.OnWillQuit(func(*mygo.QuitEvent) {
		t.Destroy()
		a.tray = nil
	})
	t.OnClick(a.showWindow)
	a.updateTray()
}

// showWindow focuses the window, creating it if the tray was all that showed.
func (a *App) showWindow() {
	if a.win != nil {
		a.win.Show()
		a.win.Focus()
		return
	}
	bg := "#1F1E1D"
	if a.themeName == "light" {
		bg = "#FAF9F5"
	}
	// A fixed 1440x900 overflowed a 1440x900 laptop screen (the taskbar takes part of
	// it): fit the restored size to the work area, open maximized the first time, and
	// let StateKey bring back whatever the user left it at on later launches.
	w, h := 1440, 900
	if wa := mygo.Screen.PrimaryDisplay().WorkArea; wa.Width > 0 && wa.Height > 0 {
		w, h = min(w, wa.Width-64), min(h, wa.Height-48)
	}
	win := mygo.NewWindow(mygo.WindowOptions{
		Title:           "Colab-yoga",
		Width:           w,
		Height:          h,
		MinWidth:        min(1000, w),
		MinHeight:       min(640, h),
		Maximized:       true,
		StateKey:        "main",
		BackgroundColor: bg,
		Content:         ui.View(a.view),
	})
	a.win = win
	// Closing destroys the window, which gives its renderer's memory back; App and the
	// background loop stay, slowed down, and the tray brings the window back.
	win.OnClosed(func() {
		if a.win == win {
			a.win = nil
		}
		a.trayHintOnce()
		// Once the renderer's buffers are gone, hand the freed heap back and let Windows
		// trim the working set, so the tray-only process stays small.
		go func() {
			time.Sleep(3 * time.Second)
			debug.FreeOSMemory()
			trimWorkingSet()
		}()
	})
}

func (a *App) trayHintOnce() {
	if num(a.set.Load(), "consoleTrayHintShown", 0) > 0 {
		return
	}
	_ = a.set.Set(map[string]any{"consoleTrayHintShown": 1})
	go func() {
		n := mygo.NewNotification(mygo.NotificationOptions{
			Title: "Colab-yoga 还在托盘里",
			Body:  "关窗口不影响 GPU：后台保活照常运行，快自动释放时会提醒你。点托盘图标重新打开，右键可退出。",
		})
		_ = n.Show()
	}()
}
