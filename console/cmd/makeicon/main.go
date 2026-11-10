// Command makeicon draws the console's app icon with MyGo's own painter and writes
// resources/icon.png (1024×1024), which `mygo build` turns into the exe's icon resource:
// Colab's two interlocking loops redrawn in Anthropic's palette, a clay "C" and a slate
// "O" on an ivory tile.
//
//	go run ./cmd/makeicon            # writes resources/icon.png
//	go run ./cmd/makeicon -preview d # also writes d/icon-{16,24,32,48,256}.png
package main

import (
	"flag"
	"image"
	"image/png"
	"log"
	"math"
	"os"
	"path/filepath"

	"github.com/egoist/mygo/ui"
)

var (
	ivory = ui.Hex("#F0EEE6")
	edge  = ui.Hex("#E3DACC")
	clay  = ui.Hex("#D97757")
	slate = ui.Hex("#262624")
)

// draw paints the icon into r, scaled from a 1024 design grid.
func draw(p *ui.Painter, r ui.Rect) {
	s := r.W / 1024
	at := func(v float32) float32 { return v * s }
	inset := at(56)
	tile := ui.Rect{X: r.X + inset, Y: r.Y + inset, W: r.W - 2*inset, H: r.H - 2*inset}
	radius := tile.W * 0.235
	// Tile with a hairline edge so it holds on light and dark taskbars alike.
	p.Fill(tile, ivory, radius)
	p.Stroke(tile, edge, radius, at(10))

	cy := r.Y + r.H/2
	ring := at(186)  // radius of each loop
	width := at(106) // stroke width
	cxC := r.X + r.W/2 - at(124)
	cxO := r.X + r.W/2 + at(124)

	// The "O" first, then the "C" open toward it (over the O at the lower crossing),
	// then the O's upper crossing over the C again, so the loops read as linked.
	// Angles in degrees, 0 to the right, growing clockwise (y points down).
	p.StrokePath(arc(cxO, cy, ring, 0, 360).Cap(ui.RoundCap), width, slate)
	p.StrokePath(arc(cxC, cy, ring, 50, 310).Cap(ui.RoundCap), width, clay)
	p.StrokePath(arc(cxO, cy, ring, 206, 252).Cap(ui.ButtCap), width, slate)
}

// arc is a polyline along a circle from a0 to a1 degrees.
func arc(cx, cy, r, a0, a1 float32) *ui.Path {
	const steps = 96
	path := new(ui.Path)
	for i := 0; i <= steps; i++ {
		a := float64(a0+(a1-a0)*float32(i)/steps) * math.Pi / 180
		x, y := cx+r*float32(math.Cos(a)), cy+r*float32(math.Sin(a))
		if i == 0 {
			path.MoveTo(x, y)
		} else {
			path.LineTo(x, y)
		}
	}
	if a1-a0 >= 360 {
		path.Close()
	}
	return path
}

func render(size int) *image.RGBA {
	view := func(c *ui.Context) {
		t := *c.Theme()
		t.Background = ui.Transparent // the root paints the theme's background
		c.SetTheme(&t)
		ui.Box(c).Fill().Draw(draw)
	}
	return ui.Render(view, size, size, 1)
}

func renderSocial() *image.RGBA {
	view := func(c *ui.Context) {
		t := *c.Theme()
		t.Background = ui.Hex("#1F1E1D")
		c.SetTheme(&t)
		ui.Box(c).Fill().Draw(func(p *ui.Painter, r ui.Rect) {
			const side = 520
			draw(p, ui.Rect{X: r.X + (r.W-side)/2, Y: r.Y + (r.H-side)/2, W: side, H: side})
		})
	}
	return ui.Render(view, 1280, 640, 1)
}

func save(img image.Image, path string) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		log.Fatal(err)
	}
	f, err := os.Create(path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	if err := png.Encode(f, img); err != nil {
		log.Fatal(err)
	}
}

func main() {
	preview := flag.String("preview", "", "directory for small-size previews")
	social := flag.String("social", "", "output path for 1280x640 GitHub social preview PNG")
	flag.Parse()
	save(render(1024), filepath.Join("resources", "icon.png"))
	// The tray icon, embedded by main.go: 16 points, 32 pixels at 200% scaling.
	save(render(32), filepath.Join("resources", "tray.png"))
	if *social != "" {
		save(renderSocial(), *social)
	}
	if *preview != "" {
		for _, n := range []int{16, 24, 32, 48, 256} {
			save(render(n), filepath.Join(*preview, "icon-"+itoa(n)+".png"))
		}
	}
}

func itoa(n int) string {
	b := []byte{}
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
