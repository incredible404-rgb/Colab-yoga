package main

import (
	"time"

	"github.com/egoist/mygo/ui"
)

// Motion follows what Apple's HIG, Material 3 and motion.dev agree on:
//   - What moves rides a spring. A spring keeps its velocity when its target changes on
//     the way, so a second click mid-flight turns smoothly instead of restarting.
//   - What only changes color or opacity eases on a short curve marked NonMotion, so it
//     still eases (rather than jumps) when the desktop asks for less motion; positions,
//     sizes and scales then go to their place at once (MyGo does that for us).
//   - Nothing loops at rest. Pulses and shimmers run only while a job is going (wake,
//     release, model load, inference), and draw from Draw, which repaints without
//     running the view; an idle window asks for no frames at all (TestIdleWindowIsStill).
var (
	// Material 3's standard curve.
	easeStandard = ui.CubicBezier(0.2, 0, 0, 1)

	springGentle = ui.Animation{Spring: ui.SpringOptions{Duration: 520 * time.Millisecond}}               // bars and gauges
	springPop    = ui.Animation{Spring: ui.SpringOptions{Duration: 440 * time.Millisecond, Bounce: 0.22}} // nav highlight, badges
	springLift   = ui.Animation{Spring: ui.SpringOptions{Duration: 280 * time.Millisecond, Bounce: 0.2}}  // hover and press
	springEnter  = ui.Animation{Spring: ui.SpringOptions{Duration: 620 * time.Millisecond, Bounce: 0.1}}  // cards arriving
	springSize   = ui.Animation{Spring: ui.SpringOptions{Duration: 380 * time.Millisecond, Bounce: 0.08}} // widths that follow text

	fadeColor = ui.Animation{Duration: 220 * time.Millisecond, Ease: easeStandard, Oklab: true, NonMotion: true}
	fadeTheme = ui.Animation{Duration: 360 * time.Millisecond, Ease: easeStandard, NonMotion: true}

	colorMotion = ui.ElementTransition{Colors: true, Shadows: true, Animation: &fadeColor}

	// The page going away lifts and fades quickly while the next one's cards rise in. It
	// names Colors so that nothing else animates: a transition that names no property
	// animates size too, and the page's height then rode this spring whenever a card grew
	// or shrank, asking for frames for 1.3 s after a wake while nothing moved.
	pageMotion = ui.ElementTransition{
		Colors:       true,
		Animation:    &springEnter,
		Exit:         &ui.Motion{Y: -10, Blur: 3},
		ExitDuration: 110 * time.Millisecond,
		ExitEase:     ui.EaseOut, // out of the way at once: the next page is already rising
	}
	popIn = ui.ElementTransition{
		Animation: &ui.Bouncy,
		Enter:     &ui.Motion{Scale: 0.2},
		Exit:      &ui.Motion{Scale: 0.2},
	}
	rowMotion = ui.ElementTransition{
		Animation: &springEnter,
		Enter:     &ui.Motion{Collapse: true, Y: -6},
		Exit:      &ui.Motion{Collapse: true, Y: -6},
	}
	// The rows of the model card. A job in flight adds rows (its stages, its latest line)
	// and takes them away, and a transition that names no property animates position too:
	// every row below glides to its new place. On the page's spring that glide takes
	// 1.2 s to come to rest; on this shorter one the window is still within the second.
	liveMotion = ui.ElementTransition{
		Animation:    &springSize,
		Enter:        &ui.Motion{Collapse: true, Y: -6},
		Exit:         &ui.Motion{Collapse: true},
		ExitDuration: 160 * time.Millisecond,
		ExitEase:     ui.EaseOut,
	}
	numeric     = ui.ContentTransition{Numeric: true}
	numericDown = ui.ContentTransition{Numeric: true, Down: true}
	morph       = ui.ContentTransition{Morph: true}
	crossfade   = ui.ContentTransition{}
)

// stagger is the delay between siblings arriving one after another.
const stagger = 55 * time.Millisecond

// arrive brings e in once, the first time it is built: lowered and a touch smaller, it
// rises into place on a spring, delay after the frame it appeared in. It does not fade
// in. On the GPU (MyGo 0.8.3, Direct3D 11) an area whose opacity animates flickers:
// recorded off a 120 Hz screen, 10 to 24 frames of a window's first second showed a
// whole card a step brighter or darker than the frames on both sides of it, plain to
// see on the dark theme; with the fade left out, none. The software renderer the tests
// draw with does not do it, so only a recording of the real window can tell.
// When hover is true it then lifts a little under the pointer. Both are poses, which
// only repaint: a staggered page coming in never runs the view frame by frame.
func arrive(c *ui.Context, e ui.Element, delay time.Duration, hover bool) ui.Element {
	// Born in this frame: a click that changes the page builds the view twice before it
	// paints, so "first build" alone would pose it at rest before anyone saw it hidden.
	born := ui.Local(e, "arrived", func() time.Time { return c.Now() })
	pose, a := ui.Pose{}, springLift
	switch {
	case c.Now().Equal(*born):
		pose = ui.Pose{Y: 18, Scale: 0.985}
		c.Invalidate() // the next build poses it at rest, and the spring takes it there
	case c.Now().Sub(*born) < delay+springEnter.Spring.Duration:
		a = springEnter
		a.Delay = delay
	case hover && e.Hovered():
		pose = ui.Pose{Y: -2}
	}
	return e.AnimatePose(pose, a)
}

// hoverLift raises a tile 2 DIPs under the pointer and lets a soft shadow grow beneath
// it; the lift is a pose and the shadow a transition, so neither runs the view per frame.
func hoverLift(e ui.Element, p Palette) ui.Element {
	pose := ui.Pose{}
	if e.Hovered() {
		pose.Y = -2
		e.Shadow(0, 6, 18, -6, ui.RGBA(0, 0, 0, map[bool]float32{true: 0.45, false: 0.14}[p.Dark]))
	}
	return e.Transition(colorMotion).AnimatePose(pose, springLift)
}

// blendPalette crossfades the two themes, t from 0 (dark) to 1 (light), in Oklab so
// the ivory and slate ramps pass through clean mid-tones rather than a muddy gray.
func blendPalette(t float32) Palette {
	switch {
	case t <= 0:
		return darkPalette
	case t >= 1:
		return lightPalette
	}
	d, l := darkPalette, lightPalette
	m := func(a, b ui.Color) ui.Color { return a.MixOklab(b, t) }
	return Palette{
		Dark: t < 0.5,
		Bg:   m(d.Bg, l.Bg), Panel: m(d.Panel, l.Panel), Card: m(d.Card, l.Card),
		Raised: m(d.Raised, l.Raised), Hover: m(d.Hover, l.Hover),
		Border: m(d.Border, l.Border), BorderStrong: m(d.BorderStrong, l.BorderStrong),
		Text: m(d.Text, l.Text), Text2: m(d.Text2, l.Text2), Text3: m(d.Text3, l.Text3),
		Accent: m(d.Accent, l.Accent), AccentHover: m(d.AccentHover, l.AccentHover), AccentText: m(d.AccentText, l.AccentText),
		Ink: m(d.Ink, l.Ink), InkHover: m(d.InkHover, l.InkHover), InkText: m(d.InkText, l.InkText),
		Success: m(d.Success, l.Success), Warning: m(d.Warning, l.Warning), Danger: m(d.Danger, l.Danger),
		Info: m(d.Info, l.Info), Muted: m(d.Muted, l.Muted),
		Off:       m(d.Off, l.Off),
		ChartVRAM: m(d.ChartVRAM, l.ChartVRAM), ChartRAM: m(d.ChartRAM, l.ChartRAM), ChartDisk: m(d.ChartDisk, l.ChartDisk),
	}
}

// statusDot is a status light whose color eases between states. With pulse it sends
// out a ring every 1.6 s, drawn from Draw at 30 fps (repaint only), for as long as a
// job runs; without it, it is still and costs nothing.
func statusDot(c *ui.Context, col ui.Color, size float32, pulse bool) ui.Element {
	d := ui.Box(c).Size(size, size)
	cc := d.AnimateColor("c", col, fadeColor)
	return d.Draw(func(pt *ui.Painter, r ui.Rect) {
		cx, cy, rad := r.X+r.W/2, r.Y+r.H/2, r.W/2
		if pulse && !pt.Preferences().ReduceMotion {
			pt.AnimationFrameRate(30)
			for _, phase := range []int64{0, 800} {
				t := float32((pt.Now().UnixMilli()+phase)%1600) / 1600
				e := ui.EaseOut(t)
				rr := rad + e*rad*1.6
				pt.Fill(ui.Rect{X: cx - rr, Y: cy - rr, W: 2 * rr, H: 2 * rr}, cc.Alpha(0.38*(1-e)), rr)
			}
		} else {
			halo := rad + 2
			pt.Fill(ui.Rect{X: cx - halo, Y: cy - halo, W: 2 * halo, H: 2 * halo}, cc.Alpha(0.22), halo)
		}
		pt.Fill(r, cc, rad)
	})
}

// gauge is a progress bar whose fill springs to each new value and whose color eases;
// a negative value shows a shimmer sweeping along it (work of unknown length).
func gauge(c *ui.Context, p Palette, key string, value float64, col ui.Color, label string) ui.Element {
	track := ui.Box(c.Key(key)).Height(6).FillWidth().Role(ui.RoleProgress).Label(label)
	v := float32(-1)
	if value >= 0 {
		v = track.AnimateTo("v", float32(clamp01(value)), springGentle)
	}
	cc := track.AnimateColor("c", col, fadeColor)
	bg := p.Hover
	return track.Draw(func(pt *ui.Painter, r ui.Rect) {
		rad := r.H / 2
		pt.Fill(r, bg, rad)
		if v < 0 {
			sweep(pt, r, cc, rad)
			return
		}
		if v > 0.002 {
			pt.Fill(ui.Rect{X: r.X, Y: r.Y, W: max(r.H, r.W*v), H: r.H}, cc, rad)
		}
	})
}

// sweep paints a band of color travelling along r, the way a skeleton or an
// indeterminate bar shows work going on.
func sweep(pt *ui.Painter, r ui.Rect, col ui.Color, rad float32) {
	if pt.Preferences().ReduceMotion {
		pt.Fill(r, col.Alpha(0.5), rad)
		return
	}
	pt.AnimationFrame()
	t := easeStandard(float32(pt.Now().UnixMilli()%1400) / 1400)
	w := r.W * 0.35
	x := r.X - w + (r.W+w)*t
	pt.Clip(r, rad, func() {
		pt.FillGradient(ui.Rect{X: x, Y: r.Y, W: w, H: r.H}, ui.LinearGradient{
			Angle: 90, Stops: []ui.GradientStop{{Color: col.Alpha(0), At: 0}, {Color: col, At: 0.5}, {Color: col.Alpha(0), At: 1}},
		}, 0)
	})
}
