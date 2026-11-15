package raster

import (
	"atlas-render/internal/geometry"
	"atlas-render/internal/model"
	"context"
	"image"
	"image/color"
	"math"
	"testing"
)

func snap(doc model.Document) model.Snapshot {
	return model.Snapshot{ID: "snap-test", State: "active", Document: doc, Assets: map[string]model.Asset{}}
}

func solid(c string) model.Paint { return model.Paint{Kind: "solid", Color: c} }

func identity() model.Transform { return model.Transform{ScaleX: 1, ScaleY: 1} }

func renderPlan(t *testing.T, s model.Snapshot, scale int) (Plan, *image.NRGBA) {
	t.Helper()
	plan, e := Compile(s, scale)
	if e != nil {
		t.Fatalf("compile: %v", e)
	}
	img, e := plan.Render(context.Background())
	if e != nil {
		t.Fatalf("render: %v", e)
	}
	return plan, img
}

func eq(t *testing.T, got color.NRGBA, r, g, b, a uint8, label string) {
	t.Helper()
	want := color.NRGBA{R: r, G: g, B: b, A: a}
	if got != want {
		t.Errorf("%s: got %v want %v", label, got, want)
	}
}

// A stroked closed shape paints both the fill and the outline in one layer.
func TestStrokeFillAndOutline(t *testing.T) {
	doc := model.Document{Width: 40, Height: 20, Background: "#FFFFFF", Layers: []model.Layer{{
		ID: "box", Kind: "rectangle", Width: 20, Height: 10,
		Transform: model.Transform{X: 10, Y: 5, ScaleX: 1, ScaleY: 1},
		Opacity:   1, Fill: solid("#0000FF"),
		Stroke: &model.Stroke{Width: 6, Color: "#FF0000"},
	}}}
	_, img := renderPlan(t, snap(doc), 1)
	// Interior stays the fill color.
	eq(t, img.NRGBAAt(20, 10), 0, 0, 255, 255, "fill interior")
	// Edge column sits inside the 3-unit half-width band: stroke over fill.
	eq(t, img.NRGBAAt(11, 10), 255, 0, 0, 255, "outline over fill")
	// The band also covers pixels outside the shape boundary.
	eq(t, img.NRGBAAt(8, 10), 255, 0, 0, 255, "outline over background")
	// Far outside is untouched.
	eq(t, img.NRGBAAt(2, 10), 255, 255, 255, 255, "background untouched")
	// Stroke half width is 3: pixels at distance 4.5 are outside the band.
	eq(t, img.NRGBAAt(34, 10), 255, 255, 255, 255, "background beyond band")
}

// thickness walks through the midpoint of a rectangle's left edge along
// the world-space edge normal and counts stroke-colored world units.
func thickness(t *testing.T, sx, sy, deg float64) float64 {
	t.Helper()
	doc := model.Document{Width: 240, Height: 240, Background: "#FFFFFF", Layers: []model.Layer{{
		ID: "box", Kind: "rectangle", Width: 100, Height: 60,
		Transform: model.Transform{X: 120, Y: 120, ScaleX: sx, ScaleY: sy, Degrees: deg},
		Opacity:   1, Fill: solid("#0000FF"),
		Stroke: &model.Stroke{Width: 6, Color: "#FF0000"},
	}}}
	_, img := renderPlan(t, snap(doc), 1)
	world := geometry.From(doc.Layers[0].Transform)
	mid := world.Apply(model.Point{X: 0, Y: 30})
	// Local -X axis is the outward normal of the left edge; normalize it.
	nx, ny := -world.A, -world.B
	n := math.Hypot(nx, ny)
	nx, ny = nx/n, ny/n
	sample := func(wx, wy float64) color.NRGBA {
		ix, iy := int(math.Floor(wx)), int(math.Floor(wy))
		if ix < 0 || iy < 0 || ix >= 240 || iy >= 240 {
			return color.NRGBA{R: 255, G: 255, B: 255, A: 255}
		}
		return img.NRGBAAt(ix, iy)
	}
	isRed := func(c color.NRGBA) bool { return c.R == 255 && c.G == 0 && c.B == 0 }
	run := 0.0
	for d := -6.0; d <= 6.0; d += 0.25 {
		if isRed(sample(mid.X+nx*d, mid.Y+ny*d)) {
			run += 0.25
		}
	}
	return run
}

// Stroke thickness is defined in scene units and must stay constant even
// when the accumulated transform rotates and scales non-uniformly.
func TestStrokeConstantWorldWidth(t *testing.T) {
	cases := map[string]struct{ sx, sy, deg float64 }{
		"identity":    {1, 1, 0},
		"anisotropic": {2, 0.5, 0},
		"rotated":     {1, 1, 35},
		"combined":    {2, 0.5, 35},
		"reflected":   {-1.4, 0.7, -20},
	}
	base := thickness(t, 1, 1, 0)
	for name, c := range cases {
		got := thickness(t, c.sx, c.sy, c.deg)
		if math.Abs(got-base) > 1.0 {
			t.Errorf("%s: stroke thickness %.2f differs from baseline %.2f", name, got, base)
		}
		if got < 5 || got > 7 {
			t.Errorf("%s: stroke thickness %.2f outside the expected ~6 scene units", name, got)
		}
	}
}

// The outline is subject to every clip in the accumulated clip chain.
func TestStrokeObeysClipChain(t *testing.T) {
	doc := model.Document{Width: 120, Height: 120, Background: "#FFFFFF", Layers: []model.Layer{{
		ID: "g", Kind: "group", Transform: identity(), Opacity: 1,
		Clip: &model.Rect{X: 0, Y: 0, Width: 60, Height: 120},
		Children: []model.Layer{{
			ID: "ring", Kind: "ellipse", Width: 80, Height: 80,
			Transform: model.Transform{X: 20, Y: 20, ScaleX: 1, ScaleY: 1},
			Opacity:   1, Fill: solid("#0000FF"),
			Stroke: &model.Stroke{Width: 6, Color: "#FF0000"},
		}},
	}}}
	_, img := renderPlan(t, snap(doc), 1)
	eq(t, img.NRGBAAt(100, 60), 255, 255, 255, 255, "stroke clipped on the right")
	if c := img.NRGBAAt(21, 60); !(c.R == 255 && c.G == 0 && c.B == 0) {
		t.Errorf("stroke visible near left boundary inside clip, got %v", c)
	}
}

// Ellipse and polygon outlines tessellate into the same distance machinery.
func TestStrokeCurvedAndPolygon(t *testing.T) {
	doc := model.Document{Width: 120, Height: 120, Background: "#FFFFFF", Layers: []model.Layer{
		{
			ID: "disc", Kind: "ellipse", Width: 40, Height: 40,
			Transform: model.Transform{X: 10, Y: 10, ScaleX: 1.5, ScaleY: 1, Degrees: 20},
			Opacity:   1, Fill: solid("#0000FF"),
			Stroke: &model.Stroke{Width: 4, Color: "#FF0000"},
		},
		{
			ID: "tri", Kind: "polygon", Width: 40, Height: 40,
			Transform: model.Transform{X: 60, Y: 60, ScaleX: 1, ScaleY: 1},
			Opacity:   1, Fill: solid("#00FF00"),
			Stroke: &model.Stroke{Width: 4, Color: "#000000"},
			Points: []model.Point{{X: 5, Y: 35}, {X: 20, Y: 5}, {X: 35, Y: 35}},
		},
	}}
	_, img := renderPlan(t, snap(doc), 1)
	// A point just outside the triangle's bottom edge must carry the outline.
	foundBlack := false
	for y := 93; y <= 96 && !foundBlack; y++ {
		for x := 66; x <= 76; x++ {
			c := img.NRGBAAt(x, y)
			if c.R == 0 && c.G == 0 && c.B == 0 {
				foundBlack = true
			}
		}
	}
	if !foundBlack {
		t.Error("polygon outline missing near bottom edge")
	}
	// Ellipse interior remains its fill.
	if c := img.NRGBAAt(40, 30); !(c.B == 255 && c.R == 0) {
		t.Errorf("ellipse fill corrupted by stroke: %v", c)
	}
}

// Layer opacity multiplies both fill and outline alpha.
func TestStrokeOpacity(t *testing.T) {
	doc := model.Document{Width: 40, Height: 20, Background: "#FFFFFF", Layers: []model.Layer{{
		ID: "box", Kind: "rectangle", Width: 20, Height: 10,
		Transform: model.Transform{X: 10, Y: 5, ScaleX: 1, ScaleY: 1},
		Opacity:   0.5, Fill: solid("#0000FF"),
		Stroke: &model.Stroke{Width: 6, Color: "#FF0000"},
	}}}
	_, img := renderPlan(t, snap(doc), 1)
	// Red over white at 50% alpha: blend rounds to 127.
	if c := img.NRGBAAt(8, 10); c.R != 255 || c.G != 127 || c.B != 127 || c.A != 255 {
		t.Errorf("half-opacity stroke = %v, want {255 127 127 255}", c)
	}
}

// Rendering the same snapshot twice must be pixel identical.
func TestStrokeRenderingDeterministic(t *testing.T) {
	doc := model.Document{Width: 80, Height: 80, Background: "#101010", Layers: []model.Layer{{
		ID: "disc", Kind: "ellipse", Width: 50, Height: 30,
		Transform: model.Transform{X: 15, Y: 25, ScaleX: 1.3, ScaleY: 0.8, Degrees: -18},
		Opacity:   .7, Fill: solid("#145A70"),
		Stroke: &model.Stroke{Width: 5, Color: "#E87E4F"},
	}}}
	_, a := renderPlan(t, snap(doc), 2)
	_, b := renderPlan(t, snap(doc), 2)
	for y := 0; y < 160; y++ {
		for x := 0; x < 160; x++ {
			if a.NRGBAAt(x, y) != b.NRGBAAt(x, y) {
				t.Fatalf("non-deterministic pixel at %d,%d: %v vs %v", x, y, a.NRGBAAt(x, y), b.NRGBAAt(x, y))
			}
		}
	}
}

// Scenes without strokes keep their exact baseline pixel behavior and cost.
func TestNoStrokeUnchanged(t *testing.T) {
	doc := model.Document{Width: 40, Height: 20, Background: "#FFFFFF", Layers: []model.Layer{{
		ID: "box", Kind: "rectangle", Width: 20, Height: 10,
		Transform: model.Transform{X: 10, Y: 5, ScaleX: 1, ScaleY: 1},
		Opacity:   1, Fill: solid("#0000FF"),
	}}}
	plan, img := renderPlan(t, snap(doc), 1)
	if len(plan.ops[0].segments) != 0 {
		t.Error("stroke segments allocated without a stroke")
	}
	eq(t, img.NRGBAAt(15, 10), 0, 0, 255, 255, "legacy fill")
	eq(t, img.NRGBAAt(5, 10), 255, 255, 255, 255, "legacy background")
	if plan.Cost != 200 {
		t.Errorf("legacy cost = %d, want 200", plan.Cost)
	}
}

// Stroke widening is compiled into the work budget and rejected up front;
// an over-budget stroked scene must never produce an accepted plan.
func TestStrokeBudgetRejectedAtCompile(t *testing.T) {
	layers := make([]model.Layer, 0, 40)
	for i := 0; i < 40; i++ {
		layers = append(layers, model.Layer{
			ID:   "s" + string(rune('a'+i/26)) + string(rune('a'+i%26)),
			Kind: "rectangle", Width: 900, Height: 900,
			Transform: model.Transform{X: 55, Y: 55, ScaleX: 1, ScaleY: 1},
			Opacity:   1, Fill: solid("#0000FF"),
			Stroke: &model.Stroke{Width: 110, Color: "#FF0000"},
		})
	}
	doc := model.Document{Width: 1500, Height: 1500, Background: "#FFFFFF", Layers: layers}
	if _, e := Compile(snap(doc), 1); e == nil {
		t.Fatal("expected budget rejection for over-budget stroke")
	}
	// Same geometry with a narrower stroke stays within the 40M budget.
	for i := range doc.Layers {
		doc.Layers[i].Stroke.Width = 100
	}
	plan, e := Compile(snap(doc), 1)
	if e != nil {
		t.Fatalf("affordable stroke rejected: %v", e)
	}
	if plan.Cost > 40_000_000 {
		t.Errorf("plan cost %d exceeds budget", plan.Cost)
	}
}

// Stroke validation is enforced before compilation.
func TestStrokeValidation(t *testing.T) {
	base := func() model.Document {
		return model.Document{Width: 40, Height: 40, Background: "#FFFFFF", Layers: []model.Layer{{
			ID: "box", Kind: "rectangle", Width: 10, Height: 10,
			Transform: identity(), Opacity: 1, Fill: solid("#0000FF"),
		}}}
	}
	for name, st := range map[string]model.Stroke{
		"zero width": {Width: 0, Color: "#FF0000"},
		"oversized":  {Width: 4000, Color: "#FF0000"},
		"bad color":  {Width: 4, Color: "red"},
		"nonfinite":  {Width: math.NaN(), Color: "#FF0000"},
	} {
		doc := base()
		doc.Layers[0].Stroke = &st
		if _, e := Compile(snap(doc), 1); e == nil {
			t.Errorf("%s: expected validation error", name)
		}
	}
	imageDoc := base()
	imageDoc.Layers[0] = model.Layer{ID: "img", Kind: "image", Width: 10, Height: 10, Transform: identity(), Opacity: 1, AssetID: "a", Stroke: &model.Stroke{Width: 4, Color: "#FF0000"}}
	if _, e := Compile(snap(imageDoc), 1); e == nil {
		t.Error("stroke on image layer must be rejected")
	}
	groupDoc := base()
	groupDoc.Layers[0] = model.Layer{ID: "grp", Kind: "group", Transform: identity(), Opacity: 1, Stroke: &model.Stroke{Width: 4, Color: "#FF0000"}, Children: []model.Layer{{
		ID: "kid", Kind: "rectangle", Width: 5, Height: 5, Transform: identity(), Opacity: 1, Fill: solid("#0000FF"),
	}}}
	if _, e := Compile(snap(groupDoc), 1); e == nil {
		t.Error("stroke on group must be rejected")
	}
}
