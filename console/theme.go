package main

import "github.com/egoist/mygo/ui"

// Palette follows anthropic.com's design tokens (its CSS: ivory #FAF9F5 / #F0EEE6 /
// #E8E6DC, slate #141413…#5E5D59, clay #D97757 / #C6613F, sky #6A9BCC, olive #788C5D),
// with its dark theme built from the same slate ramp.
type Palette struct {
	Dark                                  bool
	Bg, Panel, Card, Raised, Hover        ui.Color
	Border, BorderStrong                  ui.Color
	Text, Text2, Text3                    ui.Color
	Accent, AccentHover, AccentText       ui.Color
	Ink, InkHover, InkText                ui.Color // primary buttons: ink on ivory, ivory on slate
	Success, Warning, Danger, Info, Muted ui.Color
	ChartVRAM, ChartRAM, ChartDisk        ui.Color
}

// Fonts installed on this machine; DirectWrite falls back along each list, CJK included.
const (
	fontSerif = "Anthropic Serif, Anthropic Serif Web Display Light, Noto Serif SC, Georgia"
	fontSans  = "Segoe UI Variable Text, Segoe UI, Noto Sans SC, Microsoft YaHei UI"
	fontMono  = "Anthropic Mono Web, Cascadia Mono, Consolas, Noto Sans SC"
)

var darkPalette = Palette{
	Dark:  true,
	Bg:    ui.Hex("#1F1E1D"), Panel: ui.Hex("#1A1918"), Card: ui.Hex("#262624"),
	Raised: ui.Hex("#30302E"), Hover: ui.Hex("#3D3D3A"),
	Border: ui.Hex("#30302E"), BorderStrong: ui.Hex("#4D4C48"),
	Text: ui.Hex("#FAF9F5"), Text2: ui.Hex("#C2C0B6"), Text3: ui.Hex("#9C9A92"),
	Accent: ui.Hex("#D97757"), AccentHover: ui.Hex("#E08A6D"), AccentText: ui.Hex("#141413"),
	Ink: ui.Hex("#FAF9F5"), InkHover: ui.Hex("#E8E6DC"), InkText: ui.Hex("#141413"),
	Success: ui.Hex("#9CB27A"), Warning: ui.Hex("#D9A55B"), Danger: ui.Hex("#E07A6A"),
	Info: ui.Hex("#90C3F6"), Muted: ui.Hex("#73726C"),
	ChartVRAM: ui.Hex("#D97757"), ChartRAM: ui.Hex("#6A9BCC"), ChartDisk: ui.Hex("#9CB27A"),
}

var lightPalette = Palette{
	Bg:    ui.Hex("#FAF9F5"), Panel: ui.Hex("#F5F4ED"), Card: ui.Hex("#FFFFFF"),
	Raised: ui.Hex("#F0EEE6"), Hover: ui.Hex("#E8E6DC"),
	Border: ui.Hex("#E8E6DC"), BorderStrong: ui.Hex("#D1CFC5"),
	Text: ui.Hex("#141413"), Text2: ui.Hex("#3D3D3A"), Text3: ui.Hex("#73726C"),
	Accent: ui.Hex("#D97757"), AccentHover: ui.Hex("#C6613F"), AccentText: ui.Hex("#FFFFFF"),
	Ink: ui.Hex("#141413"), InkHover: ui.Hex("#30302E"), InkText: ui.Hex("#FAF9F5"),
	Success: ui.Hex("#788C5D"), Warning: ui.Hex("#B5822E"), Danger: ui.Hex("#BF4D43"),
	Info: ui.Hex("#6A9BCC"), Muted: ui.Hex("#B0AEA5"),
	ChartVRAM: ui.Hex("#D97757"), ChartRAM: ui.Hex("#6A9BCC"), ChartDisk: ui.Hex("#788C5D"),
}

func (a *App) pal() Palette {
	if a.themeName == "light" {
		return lightPalette
	}
	return darkPalette
}

// applyTheme makes MyGo's own widgets (inputs, selects, dialogs, scroll bars) match.
func applyTheme(c *ui.Context, p Palette) {
	var t ui.Theme
	if p.Dark {
		t = *ui.DarkTheme()
	} else {
		t = *ui.LightTheme()
	}
	t.Background, t.Surface, t.SurfaceHover, t.SurfacePressed = p.Bg, p.Raised, p.Hover, p.BorderStrong
	t.Border, t.Text, t.TextMuted = p.BorderStrong, p.Text, p.Text3
	t.Accent, t.AccentHover, t.AccentPressed, t.AccentText = p.Accent, p.AccentHover, p.AccentHover, p.AccentText
	t.InactiveAccent = p.Accent
	t.Danger, t.Warning, t.Success = p.Danger, p.Warning, p.Success
	t.Focus = p.Accent
	t.Selection = p.Accent.Alpha(0.35)
	t.Inverse, t.InverseText = p.Ink, p.InkText
	t.Scrollbar = p.BorderStrong
	t.Radius = 8
	t.FontSize = 14
	t.Font = fontSans
	c.SetTheme(&t)
}
