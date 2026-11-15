package raster

import (
	"atlas-render/internal/codec"
	"atlas-render/internal/geometry"
	"atlas-render/internal/model"
	"atlas-render/internal/paint"
	"atlas-render/internal/scene"
	"image"
	"image/color"
	"math"
)

type clip struct {
	inverse geometry.Matrix
	rect    model.Rect
}
type operation struct {
	layer       model.Layer
	inverse     geometry.Matrix
	clips       []clip
	opacity     float64
	bounds      image.Rectangle
	bitmap      image.Image
	strokeColor color.NRGBA
	strokeHalf  float64
	segments    []geometry.Segment
}
type Plan struct {
	Width, Height, Scale int
	Background           string
	ops                  []operation
	Cost                 int64
}

// strokeSagitta bounds the world-space tessellation error of a curved
// outline to one eighth of a scene unit (a quarter output pixel at scale 2).
const strokeSagitta = 0.125
const strokeSubdivideDepth = 12

func Compile(s model.Snapshot, scale int) (Plan, error) {
	if e := scene.Validate(s.Document); e != nil {
		return Plan{}, e
	}
	if scale != 1 && scale != 2 {
		return Plan{}, model.Invalid("scale must be one or two")
	}
	p := Plan{Width: s.Document.Width * scale, Height: s.Document.Height * scale, Scale: scale, Background: s.Document.Background}
	decoded := map[string]image.Image{}
	var walk func([]model.Layer, geometry.Matrix, []clip, float64) error
	walk = func(ls []model.Layer, parent geometry.Matrix, clips []clip, alpha float64) error {
		for _, l := range ls {
			world := parent.Mul(geometry.From(l.Transform))
			inv, ok := world.Inverse()
			if !ok {
				return model.Invalid("layer %s has singular accumulated transform", l.ID)
			}
			localClips := append([]clip(nil), clips...)
			if l.Clip != nil {
				localClips = append(localClips, clip{inverse: inv, rect: *l.Clip})
			}
			a := alpha * l.Opacity
			if l.Kind == "group" {
				if e := walk(l.Children, world, localClips, a); e != nil {
					return e
				}
				continue
			}
			corners := []model.Point{{}, {X: l.Width}, {Y: l.Height}, {X: l.Width, Y: l.Height}}
			minX, minY, maxX, maxY := math.Inf(1), math.Inf(1), math.Inf(-1), math.Inf(-1)
			for _, c := range corners {
				v := world.Apply(c)
				minX = math.Min(minX, v.X)
				minY = math.Min(minY, v.Y)
				maxX = math.Max(maxX, v.X)
				maxY = math.Max(maxY, v.Y)
			}
			op := operation{layer: l, inverse: inv, clips: localClips, opacity: a}
			if l.Stroke != nil {
				op.strokeColor, _ = paint.Parse(l.Stroke.Color)
				op.strokeHalf = l.Stroke.Width / 2
				op.segments = outlineSegments(l, world, s.Document, op.strokeHalf)
				// The band around the outline extends the scanned box.
				minX -= op.strokeHalf
				minY -= op.strokeHalf
				maxX += op.strokeHalf
				maxY += op.strokeHalf
			}
			minX = math.Max(0, math.Min(float64(s.Document.Width), minX))
			maxX = math.Max(0, math.Min(float64(s.Document.Width), maxX))
			minY = math.Max(0, math.Min(float64(s.Document.Height), minY))
			maxY = math.Max(0, math.Min(float64(s.Document.Height), maxY))
			op.bounds = image.Rect(int(math.Floor(minX*float64(scale))), int(math.Floor(minY*float64(scale))), int(math.Ceil(maxX*float64(scale))), int(math.Ceil(maxY*float64(scale))))
			if l.Kind == "image" {
				asset, ok := s.Assets[l.AssetID]
				if !ok {
					return model.Invalid("missing pinned asset %s", l.AssetID)
				}
				img := decoded[l.AssetID]
				if img == nil {
					var e error
					img, e = codec.Decode(asset.PNG)
					if e != nil {
						return e
					}
					decoded[l.AssetID] = img
				}
				op.bitmap = img
			}
			p.Cost += int64(op.bounds.Dx()) * int64(op.bounds.Dy())
			if p.Cost > 40_000_000 {
				return model.Busy("scene exceeds raster work budget")
			}
			p.ops = append(p.ops, op)
		}
		return nil
	}
	if e := walk(s.Document.Layers, geometry.Identity(), nil, 1); e != nil {
		return Plan{}, e
	}
	return p, nil
}

// outlineSegments tessellates a closed shape outline into world-space
// segments. Segments whose widened band cannot touch the canvas are
// dropped so rotation and non-uniform scale stay bounded in work.
func outlineSegments(l model.Layer, world geometry.Matrix, doc model.Document, half float64) []geometry.Segment {
	visible := func(a, b model.Point) bool {
		minX := math.Min(a.X, b.X) - half
		maxX := math.Max(a.X, b.X) + half
		minY := math.Min(a.Y, b.Y) - half
		maxY := math.Max(a.Y, b.Y) + half
		return maxX >= 0 && minX <= float64(doc.Width) && maxY >= 0 && minY <= float64(doc.Height)
	}
	add := func(out []geometry.Segment, pts ...model.Point) []geometry.Segment {
		for i := 1; i < len(pts); i++ {
			if visible(pts[i-1], pts[i]) {
				out = append(out, geometry.Segment{A: pts[i-1], B: pts[i]})
			}
		}
		return out
	}
	switch l.Kind {
	case "rectangle":
		pts := []model.Point{{}, {X: l.Width}, {X: l.Width, Y: l.Height}, {Y: l.Height}, {}}
		out := make([]geometry.Segment, 0, 4)
		for i := 1; i < len(pts); i++ {
			out = add(out, world.Apply(pts[i-1]), world.Apply(pts[i]))
		}
		return out
	case "polygon":
		out := make([]geometry.Segment, 0, len(l.Points))
		for i := range l.Points {
			a := world.Apply(l.Points[i])
			b := world.Apply(l.Points[(i+1)%len(l.Points)])
			out = add(out, a, b)
		}
		return out
	case "ellipse":
		return ellipseSegments(l, world, doc, half, visible)
	default:
		return nil
	}
}

// ellipseSegments adaptively subdivides the unit ellipse arc in world
// space until each chord stays within strokeSagitta of the true curve.
func ellipseSegments(l model.Layer, world geometry.Matrix, doc model.Document, half float64, visible func(a, b model.Point) bool) []geometry.Segment {
	at := func(theta float64) model.Point {
		local := model.Point{X: l.Width / 2 * (1 + math.Cos(theta)), Y: l.Height / 2 * (1 + math.Sin(theta))}
		return world.Apply(local)
	}
	type arc struct{ t0, t1 float64 }
	queue := []arc{{0, 2 * math.Pi}}
	out := []geometry.Segment{}
	for depth := 0; len(queue) > 0; depth++ {
		next := []arc{}
		for _, q := range queue {
			a, b := at(q.t0), at(q.t1)
			mid := at((q.t0 + q.t1) / 2)
			sag := chordSagitta(a, b, mid)
			if sag > strokeSagitta && depth < strokeSubdivideDepth {
				next = append(next, arc{q.t0, (q.t0 + q.t1) / 2}, arc{(q.t0 + q.t1) / 2, q.t1})
				continue
			}
			if visible(a, b) {
				out = append(out, geometry.Segment{A: a, B: b})
			}
		}
		queue = next
	}
	return out
}

// chordSagitta is the perpendicular distance from the arc midpoint to
// the chord connecting the arc endpoints.
func chordSagitta(a, b, mid model.Point) float64 {
	vx, vy := b.X-a.X, b.Y-a.Y
	len2 := vx*vx + vy*vy
	if len2 == 0 {
		dx, dy := mid.X-a.X, mid.Y-a.Y
		return math.Sqrt(dx*dx + dy*dy)
	}
	t := ((mid.X-a.X)*vx + (mid.Y-a.Y)*vy) / len2
	px, py := a.X+t*vx, a.Y+t*vy
	dx, dy := mid.X-px, mid.Y-py
	return math.Sqrt(dx*dx + dy*dy)
}
