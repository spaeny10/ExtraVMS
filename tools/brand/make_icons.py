"""Axiom Vision icons from the logo's O: a teal 3/4 ring with a burst of teal and gray bars in the upper-left gap.

Writes favicon.svg (the mark alone, for browser tabs) and PNG app icons (the mark on the app's dark background) into
the folder given as argv[1].
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

TEAL = (113, 201, 180)      # sampled from axiom.webp
GRAY = (84, 89, 87)
BG = (13, 17, 23)           # the app's background (#0d1117)

# the mark in a 100 x 100 box, angles in degrees clockwise from 12 o'clock
C = 50.0
RING_IN, RING_OUT = 24.0, 40.0
RING_FROM, RING_TO = 6.0, 264.0
BARS = [  # (angle, inner r, outer r, half-width in degrees, colour)
    (272, 27, 40, 2.6, TEAL), (283, 31, 50, 2.4, GRAY), (294, 27, 40, 2.6, TEAL), (305, 31, 49, 2.4, GRAY),
    (316, 27, 40, 2.6, TEAL), (327, 33, 51, 2.4, GRAY), (339, 27, 40, 2.6, TEAL), (351, 27, 40, 2.6, TEAL),
]


def pt(angle, r, scale=1.0, off=(0.0, 0.0)):
    a = math.radians(angle)
    return (off[0] + (C + r * math.sin(a)) * scale, off[1] + (C - r * math.cos(a)) * scale)


def ring_polygon(scale=1.0, off=(0.0, 0.0), steps=180):
    outer = [pt(RING_FROM + (RING_TO - RING_FROM) * i / steps, RING_OUT, scale, off) for i in range(steps + 1)]
    inner = [pt(RING_TO - (RING_TO - RING_FROM) * i / steps, RING_IN, scale, off) for i in range(steps + 1)]
    return outer + inner


def bar_polygon(angle, r0, r1, hw, scale=1.0, off=(0.0, 0.0)):
    # a bar of constant width (like the logo's), not a wedge: half-width taken at the outer radius
    w = r1 * math.radians(hw)
    a = math.radians(angle)
    nx, ny = math.cos(a), math.sin(a)   # perpendicular to the radius in screen coords
    p0, p1 = pt(angle, r0), pt(angle, r1)
    pts = [(p0[0] - nx * w, p0[1] - ny * w), (p1[0] - nx * w, p1[1] - ny * w),
           (p1[0] + nx * w, p1[1] + ny * w), (p0[0] + nx * w, p0[1] + ny * w)]
    return [(off[0] + x * scale, off[1] + y * scale) for x, y in pts]


def svg() -> str:
    def path(points):
        return "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in points) + " Z"
    hexc = lambda c: "#%02x%02x%02x" % c   # noqa: E731
    parts = [f'<path d="{path(ring_polygon(steps=72))}" fill="{hexc(TEAL)}"/>']
    parts += [f'<path d="{path(bar_polygon(a, r0, r1, hw))}" fill="{hexc(col)}"/>' for a, r0, r1, hw, col in BARS]
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{"".join(parts)}</svg>\n'


def png(size: int, mark: float, background: str) -> Image.Image:
    """`mark`: the mark's box as a share of the icon; background "rounded" (a dark rounded square on transparent),
    "full" (dark to the edges: maskable / iOS, which round it themselves) or "none"."""
    ss = 4
    S = size * ss
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if background == "full":
        d.rectangle([0, 0, S, S], fill=BG + (255,))
    elif background == "rounded":
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG + (255,))
    box = S * mark
    off = ((S - box) / 2, (S - box) / 2)
    scale = box / 100.0
    d.polygon(ring_polygon(scale, off), fill=TEAL + (255,))
    for a, r0, r1, hw, col in BARS:
        d.polygon(bar_polygon(a, r0, r1, hw, scale, off), fill=col + (255,))
    return im.resize((size, size), Image.LANCZOS)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "favicon.svg").write_text(svg(), encoding="utf-8")
    png(192, 0.84, "rounded").save(out / "icon-192.png")
    png(512, 0.84, "rounded").save(out / "icon-512.png")
    png(512, 0.62, "full").save(out / "icon-maskable-512.png")    # inside the maskable safe zone (80 % circle)
    png(180, 0.72, "full").save(out / "apple-touch-icon.png")
    png(32, 1.0, "none").save(out / "favicon-32.png")
    png(48, 1.0, "none").save(out / "favicon-48.png")
    # a preview sheet: the logo's O next to the new mark, light and dark
    sheet = Image.new("RGBA", (1100, 300), (255, 255, 255, 255))
    sheet.paste(Image.new("RGBA", (550, 300), BG + (255,)), (550, 0))
    for x, bg in ((20, None), (570, None)):
        sheet.alpha_composite(png(256, 1.0, "none"), (x, 22))
    sheet.alpha_composite(png(192, 0.78, "rounded"), (300, 54))
    sheet.alpha_composite(png(32, 1.0, "none"), (850, 134))
    sheet.alpha_composite(png(16, 1.0, "none"), (900, 142))
    sheet.save(out / "preview.png")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
