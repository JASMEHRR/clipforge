"""Deck renderer for explainer videos (explain.py), in the GLOOD deck style.

Look: Bricolage Grotesque ExtraBold headings + Figtree body (both OFL, in
assets/fonts), navy ink outlines, lime / sky / kraft / peach fills, a white
slide frame with a thick navy border and a hard offset shadow on a soft
lime-to-sky backdrop with diagonal ribbons, a navy pill kicker, a lime marker
highlight behind the key words of the heading, and a tilted sticker.

Motion is driven by the narration: every item appears when its cue word is
spoken (explain.cue_times). Cards pop, list rows slide in, numbers count up,
bars grow, flow arrows draw, the highlight sweeps in, the sticker slams down,
and the slide content pushes left on a change. Frames stream into the encoder
(ffutil.RawVideoWriter).

Public API:
  render_track(slides, bounds, w, h, fps, out_path, cfg) -> Path
    slides[i] = {"layout", "kicker", "heading", "highlight", "sticker",
                 "items": [{"label", "detail", "object", "value", "unit",
                            "picture": png | None, "at": seconds}]}
    bounds    = len(slides)+1 absolute times; slide i shows [bounds[i], bounds[i+1]).
  sticker_time(items, start, end) -> float   (also used for its sound effect)
Landscape (16:9) only for now.
"""
from __future__ import annotations

import math
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

INK = (15, 35, 64)
LIME = (200, 245, 58)
SKY = (154, 211, 255)
KRAFT = (242, 223, 184)
PEACH = (255, 177, 153)
BLUE = (31, 111, 209)
WHITE = (255, 255, 255)
FILLS = (SKY, LIME, KRAFT, PEACH)
PUSH_S = 0.5                     # content push between slides
POP_S = 0.45                     # item pop-in
_FONT_DIR = ROOT / "assets" / "fonts"
_FAMILIES = {   # variable fonts: axes in the order the font declares them
    "display": ("BricolageGrotesque.ttf", lambda wt: [96, wt, 92]),   # opsz, wght, wdth
    "body": ("Figtree.ttf", lambda wt: [wt]),
}


# ====================================================================== text

@lru_cache(maxsize=128)
def font(family: str, weight: int, px: int):
    from PIL import ImageFont
    file, axes = _FAMILIES[family]
    f = ImageFont.truetype(str(_FONT_DIR / file), max(8, int(px)))
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
               color: tuple, max_w: float, max_lines: int, leading: float = 1.15) -> float:
    """Left-aligned wrapped text on a PIL image; returns the y below it."""
    from PIL import ImageDraw
    if not text.strip():
        return y
    lines, px = wrap(text, family, weight, px, max_w, max_lines)
    d = ImageDraw.Draw(img)
    f = font(family, weight, px)
    for ln in lines:
        d.text((x, y), ln, font=f, fill=color + (255,), anchor="la")
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
    """Pure. The sticker lands after the last item, inside the slide."""
    last = max((float(it["at"]) for it in items), default=start)
    return max(start + 0.8, min(last + 0.8, end - 0.7))


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


def _icon_box(path: str | None, side: int, u: float, number: int | None = None):
    """White rounded square with a navy border holding an icon (GLOOD style),
    or the item's number when it has no icon."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, side - 1, side - 1), int(side * 0.22), fill=WHITE + (255,),
                        outline=INK + (255,), width=max(2, int(4 * u)))
    pic = _picture(path, int(side * 0.74), int(side * 0.74))
    if pic is not None:
        img.alpha_composite(pic, ((side - pic.width) // 2, (side - pic.height) // 2))
    elif number is not None:
        d.text((side / 2, side / 2), str(number), font=font("display", 800, int(side * 0.55)),
               fill=INK + (255,), anchor="mm")
    return img


def card_content(item: dict, w: int, u: float, wide_pic: bool):
    """Card content (transparent PIL image, used height, y of the count-up
    number or None). AI drawings get a big picture area; icons sit in a
    small white box like GLOOD's."""
    from PIL import Image
    pad = int(34 * u)
    img = Image.new("RGBA", (w, int(1400 * u)), (0, 0, 0, 0))
    y = pad
    if item.get("picture"):
        if item.get("object") and wide_pic:
            pic = _picture(item["picture"], w - 2 * pad, int(250 * u))
            if pic is not None:
                img.alpha_composite(pic, ((w - pic.width) // 2, y))
                y += pic.height + int(18 * u)
        else:
            box = _icon_box(item["picture"], int(118 * u), u)
            img.alpha_composite(box, (pad, y))
            y += box.height + int(20 * u)
    num_y = None
    if has_value(item):
        num_y = y
        y += int(112 * u)
    big = not item.get("picture") and num_y is None      # text-only: let the words carry it
    y = draw_block(img, pad, y, item["label"], "display", 800, int((68 if big else 52) * u),
                   INK, w - 2 * pad, 3 if big else 2, 1.05)
    y = draw_block(img, pad, y + int(8 * u), item.get("detail", ""), "body", 500,
                   int(31 * u), INK, w - 2 * pad, 3, 1.3)
    return img, int(y + pad), num_y


def card_sprite(content, w: int, h: int, fill: tuple, u: float) -> np.ndarray:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), int(26 * u),
                                          fill=fill + (255,), outline=INK + (255,),
                                          width=max(2, int(4 * u)))
    img.alpha_composite(content.crop((0, 0, w, min(h, content.height))), (0, 0))
    return pil_to_sprite(img)


def row_sprite(item: dict, w: int, h: int, fill: tuple, u: float,
               number: int | None = None) -> np.ndarray:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), int(24 * u),
                                          fill=fill + (255,), outline=INK + (255,),
                                          width=max(2, int(4 * u)))
    side = int(min(h * 0.72, 110 * u))
    pad = int(28 * u)
    img.alpha_composite(_icon_box(item.get("picture"), side, u, number),
                        (pad, (h - side) // 2))
    tx = pad + side + int(28 * u)
    has_detail = bool(item.get("detail", "").strip())
    y = h / 2 - (int(48 * u) if has_detail else int(26 * u))
    y = draw_block(img, tx, y, item["label"], "display", 800, int(48 * u), INK,
                   w - tx - pad, 1)
    draw_block(img, tx, y + int(2 * u), item.get("detail", ""), "body", 500, int(30 * u),
               INK, w - tx - pad, 1)
    return pil_to_sprite(img)


def pill_sprite(text: str, u: float) -> np.ndarray:
    """Navy kicker pill with lime caps."""
    from PIL import Image, ImageDraw
    f = font("body", 800, int(27 * u))
    t = text.upper()
    tw = _measure(t, f)
    px, py = int(24 * u), int(11 * u)
    hgt = int(27 * u * 1.25) + 2 * py
    img = Image.new("RGBA", (int(tw) + 2 * px, hgt), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, img.width - 1, hgt - 1), hgt // 2, fill=INK + (255,))
    d.text((px, hgt / 2), t, font=f, fill=LIME + (255,), anchor="lm")
    return pil_to_sprite(img)


def sticker_sprite(text: str, u: float) -> np.ndarray:
    """Tilted white pill with a navy outline and hard shadow ('John agreed!')."""
    from PIL import Image, ImageDraw
    f = font("display", 800, int(46 * u))
    tw = _measure(text, f)
    px, py, sh = int(30 * u), int(16 * u), int(8 * u)
    hgt = int(46 * u * 1.2) + 2 * py
    wid = int(tw) + 2 * px
    img = Image.new("RGBA", (wid + sh + 2, hgt + sh + 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((sh, sh, sh + wid, sh + hgt), hgt // 2, fill=INK + (255,))
    d.rounded_rectangle((0, 0, wid, hgt), hgt // 2, fill=WHITE + (255,),
                        outline=INK + (255,), width=max(2, int(5 * u)))
    d.text((px, hgt / 2), text, font=f, fill=INK + (255,), anchor="lm")
    return transform(pil_to_sprite(img), 1.0, 6.0)


# ====================================================================== page

class Page:
    """Static backdrop + slide frame, and the frame's geometry."""

    def __init__(self, w: int, h: int):
        from PIL import Image, ImageDraw
        self.w, self.h = w, h
        self.u = u = h / 1080
        self.fx0, self.fx1 = int(0.065 * w), int(0.935 * w)
        self.fy0, self.fy1 = int(0.045 * h), int(0.925 * h)
        self.border = max(3, int(6 * u))
        self.pad = int(70 * u)
        # backdrop: lime tint top-left to sky tint bottom-right
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        t = np.clip((xx / w * 0.6 + yy / h * 0.4), 0, 1)[..., None]
        a, b = np.array((232, 246, 206), np.float32), np.array((206, 229, 251), np.float32)
        img = Image.fromarray((a * (1 - t) + b * t).astype(np.uint8)).convert("RGBA")
        d = ImageDraw.Draw(img)
        band = int(0.075 * h)
        for (x0, y0, x1, y1), cap in (((-0.05 * w, 0.16 * h, 0.25 * w, 0.27 * h), (0.02, 0.215, 0.11, 0.235)),
                                      ((0.78 * w, 0.60 * h, 1.05 * w, 0.73 * h), (0.90, 0.66, 0.99, 0.69))):
            d.line((x0, y0, x1, y1), fill=(206, 240, 120, 255), width=band)
            cx0, cy0, cx1, cy1 = cap[0] * w, cap[1] * h, cap[2] * w, cap[3] * h
            r = int(0.03 * h)
            d.line((cx0, cy0, cx1, cy1), fill=BLUE + (255,), width=2 * r)
            for cx, cy in ((cx0, cy0), (cx1, cy1)):
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLUE + (255,))
        rad, so = int(46 * u), (int(18 * u), int(22 * u))
        d.rounded_rectangle((self.fx0 + so[0], self.fy0 + so[1], self.fx1 + so[0],
                             self.fy1 + so[1]), rad, fill=INK + (255,))
        d.rounded_rectangle((self.fx0, self.fy0, self.fx1, self.fy1), rad,
                            fill=WHITE + (255,), outline=INK + (255,), width=self.border)
        self.base = np.ascontiguousarray(np.asarray(img.convert("RGB")))
        # content may move inside this rect (pushes are clipped to it)
        inset = rad // 2
        self.inner = (self.fx0 + inset, self.fy0 + inset, self.fx1 - inset, self.fy1 - inset)
        self.cx0, self.cx1 = self.fx0 + self.pad, self.fx1 - self.pad
        self.cy0, self.cy1 = int(self.fy0 + 0.25 * h), int(self.fy1 - 0.05 * h)


class Slide:
    """Pre-built sprites + geometry for one slide; draw(frame, t) animates it."""

    def __init__(self, spec: dict, page: Page, index: int, total: int):
        if spec["layout"] not in EXPLAIN_LAYOUTS:
            raise AnimateError(f"unknown slide layout '{spec['layout']}'")
        self.spec, self.p, self.index = spec, page, index
        p, u = page, page.u
        self.u = u
        self.layout = spec["layout"]
        self.items = spec["items"]
        n = max(1, len(self.items))
        cw = p.cx1 - p.cx0
        self.number = text_sprite(f"{index + 1:02d}", "body", 800, int(34 * u), BLUE)
        self.kicker = pill_sprite(spec.get("kicker") or self.layout, u)
        # heading: one line, shrunk to fit; highlight span measured in that font
        heading = spec["heading"]
        px = int((132 if self.layout == "title" else 112) * u)
        while px > 40 * u and _measure(heading, font("display", 800, px)) > cw:
            px = int(px * 0.94)
        f = font("display", 800, px)
        self.heading = text_sprite(heading, "display", 800, px, INK)
        a, b = highlight_span(heading, spec.get("highlight", ""))
        self.hl = (_measure(heading[:a], f), _measure(heading[:b], f))
        self.head_x0 = p.cx0
        self.head_y = p.fy0 + p.pad + self.kicker.shape[0] + int(20 * u)
        self.sticker = sticker_sprite(spec["sticker"], u) if spec.get("sticker", "").strip() \
            else None
        self.parts: list[tuple] = []      # (sprite, cx, cy, at, style, number spot | None, item)

        if self.layout in ("title", "compare", "flow"):
            gap = int((96 if self.layout == "flow" else 30) * u)
            w_card = int((cw - gap * (n - 1)) / n)
            wide = any(it.get("object") for it in self.items)
            built = [card_content(it, w_card, u, wide) for it in self.items]
            avail = p.cy1 - p.cy0
            need = max(hh for _, hh, _ in built)
            floor = (0.45 if self.layout == "flow" else 0.62) * avail   # no thin strips
            h_card = int(min(avail, max(need, floor)))
            top = p.cy0 + (avail - h_card) / 2 if self.layout == "flow" else p.cy0
            self.centres = []
            for k, (it, (content, _, num_y)) in enumerate(zip(self.items, built)):
                x = p.cx0 + k * (w_card + gap)
                spr = card_sprite(content, w_card, h_card, FILLS[(index + k) % len(FILLS)], u)
                spot = (x + int(34 * u), top + num_y) if num_y is not None else None
                self.parts.append((spr, x + w_card / 2, top + h_card / 2, it["at"], "pop", spot, it))
                self.centres.append((x, x + w_card))
            self.mid_y = top + h_card / 2
        elif self.layout == "list":
            gap = int(22 * u)
            avail = p.cy1 - p.cy0
            rh = int(min(150 * u, (avail - gap * (n - 1)) / n))
            for k, it in enumerate(self.items):
                spr = row_sprite(it, cw, rh, FILLS[(index + k) % len(FILLS)], u, k + 1)
                self.parts.append((spr, p.cx0 + cw / 2, p.cy0 + rh / 2 + k * (rh + gap),
                                   it["at"], "slide", None, it))
        elif self.layout == "big_number":
            it = self.items[0]
            has_pic = bool(it.get("picture"))
            w_num = int(cw * (0.60 if has_pic else 1.0))
            h_box = p.cy1 - p.cy0
            from PIL import Image
            content = Image.new("RGBA", (w_num, h_box), (0, 0, 0, 0))
            pad = int(44 * u)
            y = draw_block(content, pad, pad, it["label"], "display", 800, int(60 * u), INK,
                           w_num - 2 * pad, 2, 1.05)
            self.num_spot = (p.cx0 + pad, p.cy0 + y + int(10 * u))
            draw_block(content, pad, y + int(270 * u), it.get("detail", ""), "body", 500,
                       int(36 * u), INK, w_num - 2 * pad, 3, 1.3)
            spr = card_sprite(content, w_num, h_box, LIME, u)
            self.parts.append((spr, p.cx0 + w_num / 2, p.cy0 + h_box / 2, it["at"], "pop", None, it))
            if has_pic:
                w_pic = cw - w_num - int(30 * u)
                box = Image.new("RGBA", (w_pic, h_box), (0, 0, 0, 0))
                pic = _picture(it["picture"], int(w_pic * 0.8), int(h_box * 0.8))
                if pic is not None:
                    box.alpha_composite(pic, ((w_pic - pic.width) // 2, (h_box - pic.height) // 2))
                spr2 = card_sprite(box, w_pic, h_box, SKY, u)
                self.parts.append((spr2, p.cx1 - w_pic / 2, p.cy0 + h_box / 2,
                                   it["at"] + 0.15, "pop", None, it))
        elif self.layout == "bars":
            self.max_v = max((abs(float(it["value"])) for it in self.items), default=1) or 1
            chart_w = cw * (0.8 if self.sticker is not None else 1.0)   # sticker corner stays free
            self.chart_x1 = int(p.cx0 + chart_w)
            self.bar_w = int(min(0.16 * p.w, chart_w / n * 0.5))
            self.bar_x = [p.cx0 + chart_w * (k + 0.5) / n for k in range(n)]
            self.base_y = p.cy1 - int(64 * u)
            self.bar_h = self.base_y - (p.cy0 + int(80 * u))
            self.bar_labels = [text_sprite(it["label"], "body", 700, int(34 * u), INK)
                               for it in self.items]

    # ------------------------------------------------------------------ draw
    def draw(self, frame: np.ndarray, t: float, start: float, end: float) -> None:
        import cv2
        p, u = self.p, self.u
        blit(frame, self.number, p.fx1 - p.pad * 0.7 - self.number.shape[1] / 2,
             p.fy0 + p.pad * 0.75)
        e = ease_out_cubic((t - start) / 0.4)
        ky = p.fy0 + p.pad + self.kicker.shape[0] / 2
        blit(frame, self.kicker, self.head_x0 + self.kicker.shape[1] / 2, ky + (1 - e) * 20 * u)
        hh = self.heading.shape[0]
        hy = self.head_y + hh / 2 + (1 - e) * 30 * u
        # lime marker sweeps in behind the key words
        sweep = ease_out_cubic((t - start - 0.35) / 0.5)
        if sweep > 0:
            x0, x1 = self.head_x0 + self.hl[0] - 8 * u, self.head_x0 + self.hl[1] + 8 * u
            cv2.rectangle(frame, (int(x0), int(hy - hh * 0.02)),
                          (int(x0 + (x1 - x0) * sweep), int(hy + hh * 0.46)), LIME, -1)
        blit(frame, self.heading, self.head_x0 + self.heading.shape[1] / 2, hy)

        if self.layout == "flow":
            for k in range(1, len(self.centres)):
                q = clamp01((t - (self.items[k]["at"] - 0.35)) / 0.35)
                if q <= 0:
                    continue
                xa = self.centres[k - 1][1] + 14 * u
                xb = self.centres[k][0] - 14 * u
                xe = xa + (xb - xa) * q
                y = int(self.mid_y)
                cv2.arrowedLine(frame, (int(xa), y), (int(xe), y), INK, max(4, int(9 * u)),
                                cv2.LINE_AA, tipLength=min(0.7, 34 * u / max(1.0, xe - xa)))

        for spr, cx, cy, at, style, _spot, _it in self.parts:
            age = t - at
            if age < 0:
                continue
            if style == "pop":
                s = ease_out_back(age / POP_S)
                blit(frame, transform(spr, 0.3 + 0.7 * s, 0.0), cx, cy)
            else:
                q = ease_out_cubic(age / POP_S)
                blit(frame, spr, cx - (1 - q) * 0.7 * p.w, cy)

        for _spr, _cx, _cy, at, _style, spot, it in self.parts:   # count-ups in cards
            if spot is not None and t - at > POP_S * 0.6:
                self._number(frame, it, t - at - POP_S * 0.6, spot, int(100 * u))

        if self.layout == "big_number":
            it = self.items[0]
            if t - it["at"] > POP_S * 0.5:
                self._number(frame, it, t - it["at"] - POP_S * 0.5, self.num_spot, int(230 * u))
        elif self.layout == "bars":
            cv2.rectangle(frame, (p.cx0, int(self.base_y)),
                          (self.chart_x1, int(self.base_y + 6 * u)), INK, -1)
            for k, it in enumerate(self.items):
                cx = self.bar_x[k]
                blit(frame, self.bar_labels[k], cx, self.base_y + 42 * u)
                age = t - it["at"]
                if age < 0:
                    continue
                g = ease_out_cubic(age / 0.8)
                bh = int(self.bar_h * abs(float(it["value"])) / self.max_v * g)
                if bh > 0:
                    bx0, bx1 = int(cx - self.bar_w / 2), int(cx + self.bar_w / 2)
                    cv2.rectangle(frame, (bx0, int(self.base_y - bh)), (bx1, int(self.base_y)),
                                  FILLS[k % len(FILLS)], -1)
                    cv2.rectangle(frame, (bx0, int(self.base_y - bh)), (bx1, int(self.base_y)),
                                  INK, max(2, int(4 * u)), cv2.LINE_AA)
                val = text_sprite(fmt_value(float(it["value"]) * g, it["unit"],
                                            float(it["value"])), "display", 800, int(60 * u), INK)
                blit(frame, val, cx, self.base_y - bh - 40 * u)

        if self.sticker is not None:
            ts = sticker_time(self.items, start, end)
            age = t - ts
            if age >= 0:
                s = 1 + 0.8 * (1 - ease_out_cubic(age / 0.28))     # slams down
                sh, sw = self.sticker.shape[:2]
                blit(frame, transform(self.sticker, s, 0.0), p.cx1 - sw / 2 + 20 * u,
                     p.cy1 - sh / 2 + 10 * u)

    def _number(self, frame, it: dict, age: float, spot: tuple, px: int) -> None:
        v = float(it["value"]) * ease_out_cubic(age / 1.1)
        spr = text_sprite(fmt_value(v, it["unit"], float(it["value"])), "display", 800, px, INK)
        blit(frame, spr, spot[0] + spr.shape[1] / 2, spot[1] + spr.shape[0] / 2)


# ====================================================================== render

def render_track(slides: list[dict], bounds: list[float], w: int, h: int, fps: int,
                 out_path: str | Path, cfg: dict) -> Path:
    """Draw and encode the silent deck track (see module docstring)."""
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
    rx0, ry0, rx1, ry1 = page.inner
    built: dict[int, Slide] = {}

    def slide(i: int) -> Slide:
        if i not in built:
            for old in [k for k in built if k < i - 1]:
                del built[old]
            built[i] = Slide(slides[i], page, i, len(slides))
        return built[i]

    def draw(i: int, t: float) -> np.ndarray:
        frame = page.base.copy()
        slide(i).draw(frame, t, bounds[i], bounds[i + 1])
        return frame

    bar = max(6, int(10 * h / 1080))
    log.info("rendering %d slides, %d frames (%dx%d @ %dfps)", len(slides), n, w, h, fps)
    si = 0
    with RawVideoWriter(out_path, w, h, fps, video_encode_args(cfg)) as wr:
        for k in range(n):
            while k >= marks[si + 1]:
                si += 1
            t = t0 + k / fps
            frame = draw(si, t)
            age = t - bounds[si]
            if si > 0 and age < PUSH_S:          # content pushes left inside the frame
                old = draw(si - 1, t)
                rw = rx1 - rx0
                x = int(smoothstep(age / PUSH_S) * rw)
                frame[ry0:ry1, rx0:rx1] = np.concatenate(
                    [old[ry0:ry1, rx0 + x:rx1], frame[ry0:ry1, rx0:rx0 + x]], axis=1)
            frame[h - bar:, : int(w * (k + 1) / n)] = LIME
            frame[h - bar:h - bar + max(2, bar // 4), : int(w * (k + 1) / n)] = INK
            wr.write(np.ascontiguousarray(frame))
    return out_path
