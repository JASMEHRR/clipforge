"""Generate the ClipForge window/tab icon from the bundled Doto font.

Chromium app-mode (launcher.py --app=) takes the window and taskbar icon from
the page favicon; the app shipped none, so Edge showed its own. This renders a
dot-matrix "C" (Doto, the brand display font) in the Nothing-red accent on a
dark rounded square and writes web/favicon.ico (multi-size) + web/favicon.png.

One-off; re-run only to change the mark.

Run:  .venv\\Scripts\\python scripts\\make_favicon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "web" / "fonts" / "Doto-Variable.ttf"
WEB = ROOT / "web"

ACCENT = (215, 25, 33, 255)      # #d71921, the brand accent
BG = (11, 11, 12, 255)           # near-black tile
SIZE = 256
GLYPH = "C"


def _load_font(px: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT), px)
    try:                          # Doto is variable — pick the boldest weight
        font.set_variation_by_axes([900])
    except (OSError, AttributeError):
        pass
    return font


def _render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = round(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)

    # size the glyph to ~72% of the tile, centered on its actual ink box
    font = _load_font(round(size * 0.82))
    l, t, r, b = d.textbbox((0, 0), GLYPH, font=font)
    x = (size - (r - l)) / 2 - l
    y = (size - (b - t)) / 2 - t
    d.text((x, y), GLYPH, font=font, fill=ACCENT)
    return img


def main() -> int:
    if not FONT.exists():
        raise SystemExit(f"Doto font not found: {FONT}")
    master = _render(SIZE)
    master.save(WEB / "favicon.png")
    # .ico carries several sizes so the OS/browser picks a crisp one per context
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(WEB / "favicon.ico",
                sizes=[(s, s) for s in sizes])
    print("wrote", WEB / "favicon.png", "and", WEB / "favicon.ico",
          "sizes", sizes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
