"""Deck renderer for explainer videos (explain.py) - "money noir" look.

PJ's brief: fun, swaggering, crime-comedy / Wall-Street-party energy (all
original - no film names, characters, logos or quotes). The look:

  backdrop  warm near-black with a vignette, live film grain, gold Art Deco
            corner ornaments, a gold progress line
  type      Anton (condensed caps) headings in cream with the key words in
            gold + a gold underline swipe; Special Elite typewriter kicker
            "FILE 03 // THE CATCH"; Inter body text (all OFL / Apache-2.0,
            bundled)
  items     taped-on Polaroid / evidence cards (slightly tilted) that pop in
            on their cue word; "rap sheet" rows for lists; gold-bar charts;
            gold arrows for flows; a giant gold count-up for big numbers with
            a burst of falling dollar bills
  stamp     the slide's `sticker` slams down as a red rubber stamp
  motion    whip-pan between slides (content slides with motion blur)

Every item appears when its cue word is spoken (explain.cue_times). Frames
stream into the encoder (ffutil.RawVideoWriter).

Public API:
  render_track(slides, bounds, w, h, fps, out_path, cfg) -> Path
    slides[i] = {"layout", "kicker", "heading", "highlight", "sticker",
                 "items": [{"label", "detail", "object", "value", "unit",
                            "picture": png | None, "at": seconds}]}
  sticker_time(items, start, end) -> float   (also times its sound effect)
  money_time(slide) -> float | None          (big-number landing, for 'kaching')
Landscape (16:9) only for now.
"""
from __future__ import annotations

import math
import random
from functools import lru_cache
from pathlib import Path

import numpy as np

from animate import (AnimateError, blit, clamp01, ease_out_back, ease_out_cubic,
                     frame_marks, pil_to_sprite, smoothstep, transform)
from config import ROOT
from ffutil import RawVideoWriter, video_encode_args
from logutil import get_logger
from schemas import EXPLAIN_LAYOUTS

log = get_logger("slides")

GOLD = (226, 179, 60)
GOLD_HI = (252, 222, 130)
GOLD_DK = (150, 108, 28)
CREAM = (243, 233, 210)
PAPER = (241, 233, 214)
PAPER_DK = (226, 214, 186)
INK = (27, 24, 20)
RED = (210, 36, 52)
MUTED = (184, 172, 150)
ROW = (34, 30, 25)
PUSH_S = 0.42                    # whip-pan between slides
POP_S = 0.42                     # item pop-in
TILTS = (-2.5, 2.0, -1.5, 3.0)
_FAMILIES = {   # file, variable-font axes (None = static font)
    "head": (ROOT / "assets" / "fonts" / "Anton.ttf", None),
    "type": (ROOT / "assets" / "fonts" / "SpecialElite.ttf", None),
    "body": (ROOT / "web" / "fonts" / "Inter-Variable.ttf", lambda wt: [28, wt]),
}


# ====================================================================== text

@lru_cache(maxsize=128)
def font(family: str, weight: int, px: int):
    from PIL import ImageFont
    path, axes = _FAMILIES[family]
    f = ImageFont.truetype(str(path), max(8, int(px)))
    if axes:
        f.set_variation_by_axes(axes(weight))
    return f


def _measure(text: str, f) -> float:
    from PIL import Image, ImageDraw
    return ImageDraw.Draw(Image.new("L", (1, 1))).textlength(text, font=f)


def wrap(text: str, family: str, weight: int, px: int, max_w: float,
         max_lines: int) -> tuple[list[str], int]:
    """Greedy word wrap; shrinks the font until it fits. Returns (lines, px)."""
    words = text.split()
    while True:
        f = font(family, weight, px)
        lines, cur = [], ""
        for wd in words:
            cand = f"{cur} {wd}".strip()
            if _measure(cand, f) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        if (len(lines) <= max_lines and all(_measure(ln, f) <= max_w for ln in lines)) \
                or px <= 14:
            return lines[:max_lines], px
        px = int(px * 0.92)


def draw_block(img, x: float, y: float, text: str, family: str, weight: int, px: int,
               color: tuple, max_w: float, max_lines: int, leading: float = 1.15,
               anchor: str = "la") -> float:
    """Wrapped text on a PIL image (x = left for 'la', centre for 'ma');
    returns the y below it."""
    from PIL import ImageDraw
    if not text.strip():
        return y
    lines, px = wrap(text, family, weight, px, max_w, max_lines)
    d = ImageDraw.Draw(img)
    f = font(family, weight, px)
    for ln in lines:
        d.text((x, y), ln, font=f, fill=color + (255,), anchor=anchor)
        y += px * leading
    return y


@lru_cache(maxsize=512)
def text_sprite(text: str, family: str, weight: int, px: int, color: tuple) -> np.ndarray:
    from PIL import Image, ImageDraw
    f = font(family, weight, px)
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    img = Image.new("RGBA", (max(1, r - l + 4), max(1, b - t + 4)), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((2 - l, 2 - t), text, font=f, fill=color + (255,))
    return pil_to_sprite(img)


def fmt_value(v: float, unit: str, target: float | None = None) -> str:
    """Pure. 30000, '$' -> '$30,000'; 4.5, '%' -> '4.5%'. Decimals follow
    `target` (the final value) so a count-up to 22 never shows '21.7'."""
    ref = v if target is None else target
    s = f"{v:,.0f}" if abs(ref) >= 100 or float(ref).is_integer() else f"{v:,.1f}"
    return f"${s}" if unit == "$" else f"{s}%" if unit == "%" else s


def has_value(item: dict) -> bool:
    return item.get("unit", "none") in ("$", "%") and float(item.get("value", 0)) != 0


def highlight_span(heading: str, phrase: str) -> tuple[int, int]:
    """Pure. Character span of the highlighted words in the heading: the
    phrase if it occurs (case-insensitive), else the last word."""
    p = phrase.strip()
    i = heading.lower().find(p.lower()) if p else -1
    if i >= 0:
        return i, i + len(p)
    j = heading.rstrip().rfind(" ") + 1
    return j, len(heading.rstrip())


def sticker_time(items: list[dict], start: float, end: float) -> float:
    """Pure. The stamp lands after the last item, inside the slide."""
    last = max((float(it["at"]) for it in items), default=start)
    return max(start + 0.8, min(last + 0.8, end - 0.7))


def money_time(slide: dict) -> float | None:
    """Pure. When a big number lands (its bills start falling), else None."""
    if slide.get("layout") != "big_number" or not slide.get("items"):
        return None
    return float(slide["items"][0]["at"]) + POP_S * 0.5 + 1.0


# ====================================================================== sprites

def _picture(path: str | None, max_w: int, max_h: int):
    from PIL import Image
    if not path:
        return None
    try:
        pic = Image.open(path).convert("RGBA")
    except OSError:
        return None
    k = min(max_w / pic.width, max_h / pic.height)
    return pic.resize((max(1, int(pic.width * k)), max(1, int(pic.height * k))),
                      Image.LANCZOS)


def card_content(item: dict, w: int, u: float):
    """Polaroid / evidence-card content on paper (PIL image, used height,
    y of the count-up number or None)."""
    from PIL import Image, ImageDraw
    pad = int(26 * u)
    img = Image.new("RGBA", (w, int(1400 * u)), PAPER + (255,))
    y = pad
    if item.get("picture"):
        ph = int(230 * u if item.get("object") else 170 * u)
        ImageDraw.Draw(img).rectangle((pad, y, w - pad, y + ph), fill=PAPER_DK + (255,))
        pic = _picture(item["picture"], w - 2 * pad - int(20 * u), ph - int(20 * u))
        if pic is not None:
            img.alpha_composite(pic, ((w - pic.width) // 2, y + (ph - pic.height) // 2))
        y += ph + int(18 * u)
    num_y = None
    if has_value(item):
        num_y = y
        y += int(104 * u)
    big = not item.get("picture") and num_y is None      # text-only: the words carry it
    y = draw_block(img, pad, y, item["label"].upper(), "head", 400,
                   int((74 if big else 52) * u), INK, w - 2 * pad, 3 if big else 2, 1.08)
    y = draw_block(img, pad, y + int(6 * u), item.get("detail", ""), "body", 500,
                   int(29 * u), (70, 62, 52), w - 2 * pad, 3, 1.3)
    return img, int(y + pad), num_y


def _dress(card, angle: float, u: float) -> np.ndarray:
    """Soft shadow + a strip of tape, then the tilt; returns a sprite."""
    from PIL import Image, ImageDraw, ImageFilter
    m = int(34 * u)
    w, h = card.size
    out = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rectangle((m + int(8 * u), m + int(12 * u), m + w + int(8 * u),
                                  m + h + int(12 * u)), fill=(0, 0, 0, 150))
    out = Image.alpha_composite(out, sh.filter(ImageFilter.GaussianBlur(12 * u)))
    out.alpha_composite(card, (m, m))
    tape = Image.new("RGBA", (int(w * 0.34), int(38 * u)), (236, 224, 186, 190))
    tape = tape.rotate(-4, expand=True, resample=Image.BICUBIC)
    out.alpha_composite(tape, (m + (w - tape.width) // 2, m - tape.height // 2))
    if abs(angle) > 0.05:
        out = out.rotate(angle, expand=True, resample=Image.BICUBIC)
    return pil_to_sprite(out)


def card_sprite(content, w: int, h: int, angle: float, u: float) -> np.ndarray:
    return _dress(content.crop((0, 0, w, h)), angle, u)


def row_sprite(item: dict, w: int, h: int, number: int, u: float) -> np.ndarray:
    """'Rap sheet' row: dark slab, gold edge, gold #01, icon, caps label."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), ROW + (235,))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, int(8 * u), h), fill=GOLD + (255,))
    pad = int(30 * u)
    num = f"#{number:02d}"
    d.text((pad + int(8 * u), h / 2), num, font=font("head", 400, int(64 * u)),
           fill=GOLD + (255,), anchor="lm")
    x = pad + int(8 * u) + int(_measure(num, font("head", 400, int(64 * u)))) + int(26 * u)
    pic = _picture(item.get("picture"), int(h * 0.62), int(h * 0.62))
    if pic is not None:
        img.alpha_composite(pic, (x, (h - pic.height) // 2))
        x += pic.width + int(24 * u)
    has_detail = bool(item.get("detail", "").strip())
    y = h / 2 - (int(46 * u) if has_detail else int(28 * u))
    y = draw_block(img, x, y, item["label"].upper(), "head", 400, int(50 * u), CREAM,
                   w - x - pad, 1)
    draw_block(img, x, y + int(2 * u), item.get("detail", ""), "body", 500, int(29 * u),
               MUTED, w - x - pad, 1)
    return pil_to_sprite(img)


def stamp_sprite(text: str, u: float, seed: int) -> np.ndarray:
    """Red rubber stamp: double border, caps, ink speckle, tilted."""
    from PIL import Image, ImageDraw
    t = text.upper().strip()
    f = font("head", 400, int(70 * u))
    tw = _measure(t, f)
    px, py, b1, b2, gap = int(34 * u), int(14 * u), int(7 * u), int(3 * u), int(9 * u)
    hgt = int(70 * u * 1.25) + 2 * py
    wid = int(tw) + 2 * px
    img = Image.new("RGBA", (wid, hgt), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, wid - 1, hgt - 1), int(10 * u), outline=RED + (255,), width=b1)
    d.rounded_rectangle((b1 + gap, b1 + gap, wid - 1 - b1 - gap, hgt - 1 - b1 - gap),
                        int(6 * u), outline=RED + (255,), width=b2)
    d.text((wid / 2, hgt / 2), t, font=f, fill=RED + (255,), anchor="mm")
    arr = np.asarray(img).copy()
    rng = np.random.default_rng(seed)
    holes = rng.random(arr.shape[:2]) < 0.16                 # worn ink
    arr[..., 3] = np.where(holes, arr[..., 3] // 4, (arr[..., 3] * 0.92).astype(np.uint8))
    return transform(pil_to_sprite(Image.fromarray(arr)), 1.0, 11.0)


# ====================================================================== page

class Page:
    """Backdrop (gradient + vignette), the overlay drawn on top of every frame
    (gold Art Deco corners) and the film-grain frames."""

    def __init__(self, w: int, h: int):
        import cv2
        self.w, self.h = w, h
        self.u = u = h / 1080
        self.mx = int(0.075 * w)
        self.cx0, self.cx1 = self.mx, w - self.mx
        self.cy0, self.cy1 = int(0.31 * h), int(0.90 * h)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt(((xx - w * 0.5) / (w * 0.62)) ** 2 + ((yy - h * 0.42) / (h * 0.75)) ** 2)
        t = np.clip(r, 0, 1)[..., None] ** 1.4
        inner, outer = np.array((44, 38, 31), np.float32), np.array((9, 8, 7), np.float32)
        self.base = np.ascontiguousarray((inner * (1 - t) + outer * t).astype(np.uint8))
        # gold Art Deco corners: double L + a small diamond, kept still on top
        ov = np.zeros((h, w, 4), np.uint8)
        g = GOLD + (255,)
        L, m, th = int(150 * u), int(34 * u), max(1, int(3 * u))
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            ox = m if sx > 0 else w - 1 - m
            oy = m if sy > 0 else h - 1 - m
            for k in (0, int(12 * u)):
                x0, y0 = ox + sx * k, oy + sy * k
                cv2.line(ov, (x0, y0), (x0 + sx * (L - k), y0), g, th, cv2.LINE_AA)
                cv2.line(ov, (x0, y0), (x0, y0 + sy * (L - k)), g, th, cv2.LINE_AA)
            dx, dy = ox + sx * int(30 * u), oy + sy * int(30 * u)
            s = int(7 * u)
            cv2.fillConvexPoly(ov, np.array([[dx, dy - s], [dx + s, dy], [dx, dy + s],
                                             [dx - s, dy]]), g, cv2.LINE_AA)
        from animate import premultiply
        c = m + L + int(10 * u)                      # the ornaments live in 4 corner squares
        self.corners = [(x0 + c / 2, y0 + c / 2, premultiply(ov[y0:y0 + c, x0:x0 + c]))
                        for x0 in (0, w - c) for y0 in (0, h - c)]
        drng = random.Random(5)
        self.dust = [(drng.uniform(0, w), drng.uniform(0, h), drng.uniform(12, 40) * u,
                      max(1, int(drng.uniform(1.2, 3.0) * u)), drng.uniform(0, 6.28))
                     for _ in range(34)]
        rng = np.random.default_rng(3)
        self.grain = []
        for _ in range(6):                   # (add, sub) pairs, 3-channel
            n = rng.normal(0, 3.0, (h // 3, w // 3)).astype(np.float32)
            n = cv2.resize(n, (w, h), interpolation=cv2.INTER_LINEAR)
            pos = np.clip(n, 0, 255).astype(np.uint8)
            neg = np.clip(-n, 0, 255).astype(np.uint8)
            self.grain.append((cv2.merge((pos, pos, pos)), cv2.merge((neg, neg, neg))))

    def finish(self, frame: np.ndarray, k: int) -> np.ndarray:
        import cv2
        for cx, cy, spr in self.corners:
            blit(frame, spr, cx, cy)
        pos, neg = self.grain[(k // 3) % len(self.grain)]
        t = k / 30.0                                 # gold dust drifting up
        for j, (x0, y0, sp, r, ph) in enumerate(self.dust):
            y = (y0 - sp * t) % self.h
            x = x0 + 18 * self.u * math.sin(0.6 * t + ph)
            if math.sin(2.2 * t + ph) > -0.3:
                cv2.circle(frame, (int(x), int(y)), r, GOLD_HI, -1, cv2.LINE_AA)
        cv2.add(frame, pos, frame)
        cv2.subtract(frame, neg, frame)
        return frame


class Slide:
    """Pre-built sprites + geometry for one slide; draw(frame, t) animates it."""

    def __init__(self, spec: dict, page: Page, index: int, bill: np.ndarray | None):
        if spec["layout"] not in EXPLAIN_LAYOUTS:
            raise AnimateError(f"unknown slide layout '{spec['layout']}'")
        self.spec, self.p, self.index = spec, page, index
        p, u = page, page.u
        self.u = u
        self.layout = spec["layout"]
        self.items = spec["items"]
        self.bill = bill
        n = max(1, len(self.items))
        self.figure = None
        self.x0 = p.cx0
        fig_path = spec.get("figure")
        if fig_path:                         # the cast member stands bottom-left
            pic = _picture(fig_path, int(0.24 * p.w), int(0.62 * p.h))
            if pic is not None:
                self.figure = pil_to_sprite(pic)
                self.x0 = p.cx0 + int(0.22 * p.w)
        cw = p.cx1 - self.x0
        self.kicker = text_sprite(f"FILE {index + 1:02d} // {(spec.get('kicker') or '').upper()}",
                                  "type", 400, int(42 * u), RED)
        # heading: caps, cream with the key words in gold, shrunk to one line
        heading = spec["heading"].upper()
        a, b = highlight_span(heading, spec.get("highlight", "").upper())
        px = int((150 if self.layout == "title" else 118) * u)
        while px > 40 * u and _measure(heading, font("head", 400, px)) > p.cx1 - p.cx0:
            px = int(px * 0.94)
        self.heading, self.hl = self._heading(heading, a, b, px)
        self.head_y = int(0.085 * p.h) + self.kicker.shape[0] + int(14 * u)
        self.stamp = stamp_sprite(spec["sticker"], u, index) if spec.get("sticker", "").strip() \
            else None
        self.parts: list[tuple] = []   # (sprite, cx, cy, at, style, number spot | None, item)

        if self.layout in ("title", "compare", "flow"):
            gap = int((110 if self.layout == "flow" else 46) * u)
            w_card = int((cw - gap * (n - 1)) / n)
            built = [card_content(it, w_card, u) for it in self.items]
            avail = p.cy1 - p.cy0
            need = max(hh for _, hh, _ in built)
            floor = (0.50 if self.layout == "flow" else 0.66) * avail
            h_card = int(min(avail - 40 * u, max(need, floor)))
            top = p.cy0 + (avail - h_card) / 2
            self.centres = []
            for k, (it, (content, _, num_y)) in enumerate(zip(self.items, built)):
                x = self.x0 + k * (w_card + gap)
                tilt = 0.0 if num_y is not None else TILTS[(index + k) % len(TILTS)]
                spr = card_sprite(content, w_card, h_card, tilt, u)
                spot = (x + int(26 * u), top + num_y) if num_y is not None else None
                self.parts.append((spr, x + w_card / 2, top + h_card / 2, it["at"], "pop",
                                   spot, it))
                self.centres.append((x, x + w_card))
            self.mid_y = top + h_card / 2
        elif self.layout == "list":
            gap = int(20 * u)
            avail = p.cy1 - p.cy0
            rh = int(min(140 * u, (avail - gap * (n - 1)) / n))
            for k, it in enumerate(self.items):
                spr = row_sprite(it, cw, rh, k + 1, u)
                self.parts.append((spr, self.x0 + cw / 2, p.cy0 + rh / 2 + k * (rh + gap),
                                   it["at"], "slide", None, it))
        elif self.layout == "big_number":
            it = self.items[0]
            has_pic = bool(it.get("picture"))
            self.num_x = self.x0
            self.num_w = int(cw * (0.58 if has_pic else 1.0))
            self.big_label = text_sprite(it["label"].upper(), "type", 400, int(46 * u), MUTED)
            final = fmt_value(float(it["value"]), it["unit"], float(it["value"]))
            self.num_px = int(260 * u)               # the final number must fit its column
            while (self.num_px > 60 * u and
                   _measure(final, font("head", 400, self.num_px)) > self.num_w - 30 * u):
                self.num_px = int(self.num_px * 0.93)
            self.big_detail = None
            if it.get("detail", "").strip():
                from PIL import Image
                im = Image.new("RGBA", (self.num_w, int(200 * u)), (0, 0, 0, 0))
                draw_block(im, 0, 0, it["detail"], "body", 500, int(38 * u), CREAM,
                           self.num_w, 3, 1.3)
                self.big_detail = pil_to_sprite(im)
            if has_pic:
                w_pic = int(cw * 0.36)
                from PIL import Image as _Im
                h_pic = int(min(p.cy1 - p.cy0 - 60 * u, w_pic * 1.05))
                content = _Im.new("RGBA", (w_pic, h_pic), PAPER + (255,))
                pic = _picture(it["picture"], int(w_pic * 0.78), int(h_pic * 0.78))
                if pic is not None:                  # fill the photo, icons included
                    content.alpha_composite(pic, ((w_pic - pic.width) // 2,
                                                  (h_pic - pic.height) // 2))
                spr = card_sprite(content, w_pic, h_pic, 3.0, u)
                self.parts.append((spr, p.cx1 - w_pic / 2, p.cy0 + (p.cy1 - p.cy0) / 2,
                                   it["at"] + 0.2, "pop", None, it))
        elif self.layout == "bars":
            self.max_v = max((abs(float(it["value"])) for it in self.items), default=1) or 1
            chart_w = cw * (0.8 if self.stamp is not None else 1.0)   # stamp corner stays free
            self.chart_x1 = int(self.x0 + chart_w)
            self.bar_w = int(min(0.15 * p.w, chart_w / n * 0.48))
            self.bar_x = [self.x0 + chart_w * (k + 0.5) / n for k in range(n)]
            self.base_y = p.cy1 - int(60 * u)
            self.bar_h = self.base_y - (p.cy0 + int(90 * u))
            self.bar_labels = [text_sprite(it["label"].upper(), "head", 400, int(40 * u), CREAM)
                               for it in self.items]

    def _heading(self, text: str, a: int, b: int, px: int):
        """Heading sprite with the highlight drawn in gold; returns (sprite,
        (x0, x1) of the highlight)."""
        from PIL import Image, ImageDraw
        f = font("head", 400, px)
        d = ImageDraw.Draw(Image.new("L", (1, 1)))
        l, t, r, bt = d.textbbox((0, 0), text, font=f)
        img = Image.new("RGBA", (max(1, r - l + 8), max(1, bt - t + 8)), (0, 0, 0, 0))
        dd = ImageDraw.Draw(img)
        x = 4 - l
        for part, col in ((text[:a], CREAM), (text[a:b], GOLD), (text[b:], CREAM)):
            if part:
                dd.text((x, 4 - t), part, font=f, fill=col + (255,))
                x += _measure(part, f)
        return pil_to_sprite(img), (4 - l + _measure(text[:a], f), 4 - l + _measure(text[:b], f))

    # ------------------------------------------------------------------ draw
    def draw(self, frame: np.ndarray, t: float, start: float, end: float) -> None:
        import cv2
        p, u = self.p, self.u
        e = ease_out_cubic((t - start) / 0.45)
        kx = p.cx0 + self.kicker.shape[1] / 2
        blit(frame, self.kicker, kx, int(0.085 * p.h) + self.kicker.shape[0] / 2)
        hh, hw = self.heading.shape[:2]
        hy = self.head_y + hh / 2 + (1 - e) * 40 * u
        blit(frame, self.heading, p.cx0 + hw / 2, hy)
        sweep = ease_out_cubic((t - start - 0.4) / 0.5)
        if sweep > 0:                                # gold underline under the key words
            x0, x1 = p.cx0 + self.hl[0], p.cx0 + self.hl[1]
            y = int(hy + hh / 2 + 6 * u)
            cv2.rectangle(frame, (int(x0), y), (int(x0 + (x1 - x0) * sweep), y + int(9 * u)),
                          GOLD, -1)

        if self.layout == "flow":
            for k in range(1, len(self.centres)):
                q = clamp01((t - (self.items[k]["at"] - 0.35)) / 0.35)
                if q <= 0:
                    continue
                xa = self.centres[k - 1][1] + 16 * u
                xb = self.centres[k][0] - 16 * u
                xe = xa + (xb - xa) * q
                y = int(self.mid_y)
                cv2.arrowedLine(frame, (int(xa), y), (int(xe), y), GOLD, max(4, int(9 * u)),
                                cv2.LINE_AA, tipLength=min(0.7, 36 * u / max(1.0, xe - xa)))

        if self.figure is not None:                  # cast member: rises in, breathes, hops
            fh, fw = self.figure.shape[:2]
            q = ease_out_back((t - start - 0.15) / 0.55)
            hop = 0.0
            if self.stamp is not None:
                ha = t - sticker_time(self.items, start, end) - 0.1
                if 0 <= ha < 0.4:
                    hop = math.sin(math.pi * ha / 0.4) * 46 * u
            breathe = 1 + 0.012 * math.sin(2 * math.pi * 0.5 * t)
            fx = p.cx0 + fw / 2 - 20 * u
            fy = p.h * 0.985 - fh / 2 + (1 - q) * fh * 1.1 - hop
            blit(frame, transform(self.figure, breathe, 1.5 * math.sin(1.3 * t)), fx, fy)

        for k, (spr, cx, cy, at, style, _spot, _it) in enumerate(self.parts):
            age = t - at
            if age < 0:
                continue
            if style == "pop":                       # flies in from below, spinning down
                q = ease_out_back(age / 0.5)
                spin = (1 - clamp01(age / 0.5)) * (22 if k % 2 else -22)
                blit(frame, transform(spr, 0.85 + 0.15 * clamp01(q), spin),
                     cx, cy + (1 - q) * 0.6 * p.h)
            else:
                q = ease_out_cubic(age / POP_S)
                blit(frame, spr, cx - (1 - q) * 0.8 * p.w, cy)

        for _spr, _cx, _cy, at, _style, spot, it in self.parts:   # count-ups on cards
            if spot is not None and t - at > POP_S * 0.6:
                self._number(frame, it, t - at - POP_S * 0.6, spot, int(96 * u), INK)

        if self.layout == "big_number":
            it = self.items[0]
            age = t - it["at"]
            if age >= 0:
                y = p.cy0 + int(20 * u)
                blit(frame, self.big_label, self.num_x + self.big_label.shape[1] / 2,
                     y + self.big_label.shape[0] / 2)
                y += self.big_label.shape[0] + int(10 * u)
                spr = self._number(frame, it, age - POP_S * 0.3, (self.num_x, y), self.num_px,
                                   GOLD) if age > POP_S * 0.3 else None
                if self.big_detail is not None and spr is not None:
                    yd = y + spr.shape[0] + int(24 * u)
                    blit(frame, self.big_detail, self.num_x + self.big_detail.shape[1] / 2,
                         yd + self.big_detail.shape[0] / 2)
        elif self.layout == "bars":
            cv2.rectangle(frame, (self.x0, int(self.base_y)),
                          (self.chart_x1, int(self.base_y + 4 * u)), CREAM, -1)
            for k, it in enumerate(self.items):
                cx = self.bar_x[k]
                blit(frame, self.bar_labels[k], cx, self.base_y + 46 * u)
                age = t - it["at"]
                if age < 0:
                    continue
                g = ease_out_cubic(age / 0.8)
                bh = int(self.bar_h * abs(float(it["value"])) / self.max_v * g)
                if bh > 0:                           # a stack of gold
                    bx0, bx1 = int(cx - self.bar_w / 2), int(cx + self.bar_w / 2)
                    top = int(self.base_y - bh)
                    cv2.rectangle(frame, (bx0, top), (bx1, int(self.base_y)), GOLD, -1)
                    cv2.rectangle(frame, (bx0, top), (bx1, top + int(12 * u)), GOLD_HI, -1)
                    cv2.rectangle(frame, (bx1 - int(14 * u), top), (bx1, int(self.base_y)),
                                  GOLD_DK, -1)
                val = text_sprite(fmt_value(float(it["value"]) * g, it["unit"],
                                            float(it["value"])), "head", 400, int(66 * u), GOLD)
                blit(frame, val, cx, self.base_y - bh - 44 * u)

        mt = money_time(self.spec)
        if mt is not None and self.bill is not None and 0 <= t - mt < 3.2:
            self._money(frame, t - mt)

        if self.stamp is not None:
            age = t - sticker_time(self.items, start, end)
            if age >= 0:
                s = 1 + 1.4 * (1 - ease_out_cubic(age / 0.2))      # slams down
                sh, sw = self.stamp.shape[:2]
                sx, sy = p.cx1 - sw / 2, p.cy1 - sh / 2 + 10 * u
                cards = [pt for pt in self.parts if pt[4] == "pop"]
                if self.layout in ("title", "compare", "flow") and cards:
                    # stamp the PHOTO of the last card (evidence-style), never its text
                    spr, cx, cy = cards[-1][:3]
                    sx = min(cx + spr.shape[1] * 0.10, p.cx1 - sw / 2)
                    sy = cy - spr.shape[0] * 0.22
                blit(frame, transform(self.stamp, s, 0.0), sx, sy)

    def camera(self, t: float, start: float, end: float) -> tuple[float, float, float]:
        """(zoom, dx, dy): a slow push-in across the slide, plus a short shake
        when the stamp slams down."""
        z = 1 + 0.035 * smoothstep((t - start) / max(0.1, end - start))
        dx = dy = 0.0
        if self.stamp is not None:
            age = t - sticker_time(self.items, start, end) - 0.1
            if 0 <= age < 0.35:
                amp = 16 * self.u * math.exp(-age / 0.09)
                dx, dy = amp * math.sin(age * 95), amp * math.cos(age * 77)
        return z, dx, dy

    def _number(self, frame, it: dict, age: float, spot: tuple, px: int, color: tuple):
        v = float(it["value"]) * ease_out_cubic(age / 1.0)
        spr = text_sprite(fmt_value(v, it["unit"], float(it["value"])), "head", 400, px, color)
        blit(frame, spr, spot[0] + spr.shape[1] / 2, spot[1] + spr.shape[0] / 2)
        return spr

    def _money(self, frame, age: float) -> None:
        """A burst of dollar bills tumbling down after a big number lands."""
        rng = random.Random(self.index)
        p, u = self.p, self.u
        for _ in range(10):
            x0 = rng.uniform(0.05, 0.95) * p.w
            delay = rng.uniform(0, 0.5)
            speed = rng.uniform(420, 720) * u
            spin = rng.uniform(-200, 200)
            size = rng.uniform(0.55, 0.9)
            a = age - delay
            if a < 0:
                continue
            y = -120 * u + speed * a
            if y > p.h + 120 * u:
                continue
            x = x0 + math.sin(a * 3 + x0) * 50 * u
            blit(frame, transform(self.bill, size, spin * a), x, y)


# ====================================================================== render

def render_track(slides: list[dict], bounds: list[float], w: int, h: int, fps: int,
                 out_path: str | Path, cfg: dict) -> Path:
    """Draw and encode the silent deck track (see module docstring)."""
    import cv2
    from explain import icon_png          # lazy: explain imports this module
    if w < h:
        raise AnimateError("slides.render_track is landscape-only for now")
    if len(bounds) != len(slides) + 1 or not slides:
        raise AnimateError(f"{len(slides)} slides need {len(slides) + 1} bounds")
    marks = frame_marks(bounds, fps)
    n = marks[-1]
    t0 = bounds[0]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page = Page(w, h)
    bill = None
    bill_png = icon_png("💵", cfg)
    if bill_png:
        pic = _picture(bill_png, int(120 * page.u), int(120 * page.u))
        bill = pil_to_sprite(pic) if pic is not None else None
    built: dict[int, Slide] = {}

    def slide(i: int) -> Slide:
        if i not in built:
            for old in [k for k in built if k < i - 1]:
                del built[old]
            built[i] = Slide(slides[i], page, i, bill)
        return built[i]

    def draw(i: int, t: float) -> np.ndarray:
        frame = page.base.copy()
        sl = slide(i)
        sl.draw(frame, t, bounds[i], bounds[i + 1])
        z, dx, dy = sl.camera(t, bounds[i], bounds[i + 1])
        if z > 1.0005 or dx or dy:
            m = np.float32([[z, 0, (1 - z) * w / 2 + dx], [0, z, (1 - z) * h / 2 + dy]])
            frame = cv2.warpAffine(frame, m, (w, h), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_REFLECT)
        return frame

    bar = max(4, int(6 * h / 1080))
    log.info("rendering %d slides, %d frames (%dx%d @ %dfps)", len(slides), n, w, h, fps)
    si = 0
    enc = video_encode_args(cfg)
    cap = str((cfg.get("explain") or {}).get("max_bitrate", "")).strip()
    if cap:          # grain + constant camera motion otherwise run ~13 Mbit/s
        enc += ["-maxrate", cap, "-bufsize", cap.rstrip("Mk") + ("M" if cap.endswith("M") else "k")]
    with RawVideoWriter(out_path, w, h, fps, enc) as wr:
        for k in range(n):
            while k >= marks[si + 1]:
                si += 1
            t = t0 + k / fps
            frame = draw(si, t)
            age = t - bounds[si]
            if si > 0 and age < PUSH_S:             # whip-pan: old slides out, new in
                old = draw(si - 1, t)
                q = smoothstep(age / PUSH_S)
                x = int(q * w)
                if 0 < x < w:
                    frame = np.concatenate([old[:, x:], frame[:, :x]], axis=1)
                    blur = int(90 * math.sin(math.pi * q)) | 1
                    if blur > 3:
                        frame = cv2.blur(frame, (blur, 1))
                elif x <= 0:
                    frame = old
            frame = page.finish(frame, k)
            frame[h - bar:, : int(w * (k + 1) / n)] = GOLD
            wr.write(np.ascontiguousarray(frame))
    return out_path
