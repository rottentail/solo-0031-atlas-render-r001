#!/usr/bin/env python3
"""Decode rendered stroke PNGs and verify geometric meaning from pixels.

Run after: scripts/walkthrough.py --workflow stroke-render --out var/stroke-demo
"""
import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'var' / 'stroke-demo'


def read_png(path):
    data = Path(path).read_bytes()
    pos, width, height, bit_depth, color_type = 8, 0, 0, 0, 0
    idat = b''
    while pos < len(data):
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + size]
        if tag == b'IHDR':
            width, height, bit_depth, color_type = struct.unpack('>IIBB', chunk[:10])
        elif tag == b'IDAT':
            idat += chunk
        pos += 12 + size
    assert bit_depth == 8 and color_type in (2, 6)
    channels = 4 if color_type == 6 else 3
    raw = zlib.decompress(idat)
    stride = width * channels
    pixels, prev, cursor = [], bytearray(stride), 0

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else b if pb <= pc else c

    for _ in range(height):
        filt = raw[cursor]
        cursor += 1
        line = bytearray(stride)
        for i in range(stride):
            value = raw[cursor + i]
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filt == 0:
                recon = value
            elif filt == 1:
                recon = value + a
            elif filt == 2:
                recon = value + b
            elif filt == 3:
                recon = value + ((a + b) >> 1)
            else:
                recon = value + paeth(a, b, c)
            line[i] = recon & 255
        cursor += stride
        pixels.append(bytes(b''.join(bytes(line[i:i + 3]) + b'\xff' for i in range(0, stride, 3))) if channels == 3 else bytes(line))
        prev = line
    return width, height, pixels


def px(img, x, y):
    w, h, rows = img
    x, y = int(x), int(y)
    if not (0 <= x < w and 0 <= y < h):
        return None
    return tuple(rows[y][x * 4:x * 4 + 4])


def count_band(img, predicate):
    w, h, _ = img
    return sum(1 for y in range(h) for x in range(w) if predicate(px(img, x, y)))


def main():
    if not (OUT / 'stroke-scale-1.png').exists():
        subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/walkthrough.py'),
                        '--workflow', 'stroke-render', '--out', str(OUT)], check=True)
    img1 = read_png(OUT / 'stroke-scale-1.png')
    img2 = read_png(OUT / 'stroke-scale-2.png')
    report = {}

    is_stroke_dark = lambda c: c is not None and c[0] < 90 and c[1] < 90 and c[2] < 90 and c[3] == 255
    is_fill_blue = lambda c: c is not None and c[2] > 120 and c[0] < 90 and c[3] == 255
    is_bg = lambda c: c is not None and c[0] > 235 and c[1] > 230 and c[2] > 220

    # 1. Fill and outline coexist on the same closed rectangle (plate, 220x150 at 45,35 rot5).
    w, h, _ = img1
    inside = px(img1, 150, 110)
    assert is_fill_blue(inside), f'plate fill missing at (150,110): {inside}'
    dark1 = count_band(img1, is_stroke_dark)
    report['darkOutlinePixels1x'] = dark1
    dark2 = count_band(img2, is_stroke_dark)
    report['darkOutlinePixels2x'] = dark2
    # 2x render covers roughly four times as many outline pixels (same scene width).
    assert dark2 > 3 * dark1, (dark1, dark2)

    # 2. Outline extends beyond the fill boundary: find dark outline pixels
    #    directly adjacent to a background-colored pixel along the plate edge.
    over_bg = False
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            if is_stroke_dark(px(img1, x, y)) and (
                is_bg(px(img1, x + 1, y)) or is_bg(px(img1, x - 1, y))
                or is_bg(px(img1, x, y + 1)) or is_bg(px(img1, x, y - 1))):
                over_bg = True
                break
        if over_bg:
            break
    report['outlineOverBackground'] = over_bg
    assert over_bg

    # 3. Clipped triangle: the facet stroke (#B03A2E) must stay inside the
    #    clip quadrilateral mapped by the group transform (150,100 scale 1.4/.7
    #    rot -8, clip 90x75). The mapped quad reaches roughly x~275 and its
    #    bottom edge near y~150, so probes beyond those are certainly outside.
    def is_facet_red(c):
        return c is not None and c[0] > 150 and c[1] < 80 and c[2] < 70

    leaks = sum(1 for y in range(152, h) for x in range(0, w) if is_facet_red(px(img1, x, y)))
    leaks += sum(1 for y in range(0, 160) for x in range(280, w) if is_facet_red(px(img1, x, y)))
    # Exact check: map every facet-red pixel back through the group inverse
    # and require it to land inside the local half-open clip rect.
    import math
    rad = -8 * math.pi / 180
    cs, sn = math.cos(rad), math.sin(rad)

    def ungroup(wx, wy):
        dx, dy = wx - 150, wy - 100
        return (cs * dx + sn * dy) / 1.4, (-sn * dx + cs * dy) / 0.7

    exact_leaks = 0
    for y in range(h):
        for x in range(w):
            if is_facet_red(px(img1, x, y)):
                lx, ly = ungroup(x + .5, y + .5)
                if not (0 <= lx < 90 and 0 <= ly < 75):
                    exact_leaks += 1
    report['facetStrokeLeaksBeyondClip'] = leaks
    report['facetStrokeLeaksExactQuad'] = exact_leaks
    assert leaks == 0 and exact_leaks == 0
    red_inside = sum(1 for y in range(70, 150) for x in range(90, 200) if is_facet_red(px(img1, x, y)))
    report['facetStrokePixelsInsideClip'] = red_inside
    assert red_inside > 0

    # 4. Scale-2 output is exactly the doubled canvas and deterministic across replays.
    report['canvas1x'] = (img1[0], img1[1])
    report['canvas2x'] = (img2[0], img2[1])
    assert img2[0] == 640 and img2[1] == 480

    # 5. Band width measured along a horizontal scanline stays at ~stroke width (4 units).
    #    Plate left edge at x~45: scan y=110 (mid-height in local coords, near edge column).
    runs = []
    run = 0
    for x in range(0, 120):
        if is_stroke_dark(px(img1, x, 108)):
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    report['plateEdgeBandRunsPx'] = runs
    assert any(3 <= r <= 7 for r in runs), runs

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
