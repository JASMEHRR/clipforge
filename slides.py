"""PPT-style slide renderer for explainer videos (explain.py).

The narration drives everything: every item on a slide appears at the moment
its cue word is spoken (explain.cue_times turns Whisper word timings into
appear times). Slides push in from the right; items pop in as cards, numbers
count up, bars grow, flow arrows draw themselves, and a thin progress bar runs
along the bottom. Frames stream straight into the encoder
(ffutil.RawVideoWriter), like animate.py.

Public API:
  render_track(slides, bounds, w, h, fps, out_path, cfg) -> Path
    slides[i] = {"layout": schemas.EXPLAIN_LAYOUTS member, "heading": str,
                 "items": [{"label", "detail", "value", "unit",
                            "picture": png path | None, "at": seconds}]}
    bounds    = len(slides)+1 absolute times; slide i shows [bounds[i], bounds[i+1]).
Landscape (16:9) only for now.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np

from animate import (AnimateError, blit, clamp01, ease_out_back,
                     ease_out_cubic, frame_marks, pil_to_sprite, smoothstep, transform)
from config import ROOT
from ffutil import RawVideoWriter, video_encode_args
from logutil import get_logger
from schemas import EXPLAIN_LAYOUTS

log = get_logger("slides")

INK = (20, 33, 61)
MUTED = (92, 104, 125)
BG_TOP, BG_BOTTOM = (250, 251, 254), (229, 236, 247)
ACCENTS = ((45, 108, 223), (29, 179, 122), (255, 138, 61), (139, 92, 246), (239, 68, 68))
PUSH_S = 0.45                    # slide push transition
POP_S = 0.45                     # item pop-in


# ====================================================================== text

@lru_cache(maxsize=64)
def _font(weight: str, px: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(ROOT / "assets" / "fonts" / f"Montserrat-{weight}.ttf"),
                              max(8, int(px)))


def wrap(text: str, weight: str, px: int, max_w: int, max_lines: int) -> tuple[list[str], int]:
    """Greedy word wrap; shrinks the font until it fits max_lines. Returns
    (lines, px)."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    words = text.split()
    while True:
        f = _font(weight, px)
        lines, cur = [], ""
        for wd in words:
            cand = f"{cur} {wd}".strip()
            if d.textlength(cand, font=f) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        widest = max((d.textlength(ln, font=f) for ln in lines), default=0)
        if (len(lines) <= max_lines and widest <= max_w) or px <= 14:
            return lines[:max_lines], px
        px = int(px * 0.9)


def draw_text(img, xy, text, weight, px, color, max_w, max_lines, anchor="ma") -> int:
    """Draw wrapped text onto a PIL image at (x, y) (x = centre for 'ma',
    left for 'la'). Returns the y below the block."""
    from PIL import ImageDraw
    if not text.strip():
        return xy[1]
    lines, px = wrap(text, weight, px, max_w, max_lines)
    d = ImageDraw.Draw(img)
    f = _font(weight, px)
    y = xy[1]
    for ln in lines:
        d.text((xy[0], y), ln, font=f, fill=color + (255,), anchor=anchor)
        y += int(px * 1.22)
    return y


@lru_cache(maxsize=512)
def text_sprite(text: str, weight: str, px: int, color: tuple) -> np.ndarray:
    """One line of text as a premultiplied sprite (for headings and counters)."""
    from PIL import Image, ImageDraw
    f = _font(weight, px)
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


# ====================================================================== cards

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


def _card_base(w: int, h: int, accent: tuple, u: float):
    """White rounded card with an accent top edge and a soft shadow; returns
    (PIL RGBA image, pad) - the card body starts at (pad, pad)."""
    from PIL import Image, ImageDraw, ImageFilter
    pad, r = int(26 * u), int(26 * u)
    img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (pad, pad + int(8 * u), pad + w, pad + h + int(8 * u)), r, fill=(20, 33, 61, 60))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(12 * u)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((pad, pad, pad + w, pad + h), r, fill=accent + (255,))
    d.rounded_rectangle((pad, pad + int(10 * u), pad + w, pad + h), r, fill=(255, 255, 255, 255))
    return img, pad


def card_sprite(item: dict, w: int, h: int, accent: tuple, u: float) -> np.ndarray:
    """Vertical card: picture on top, label, detail. Text-only cards get a
    big centred label instead."""
    img, pad = _card_base(w, h, accent, u)
    pic = _picture(item.get("picture"), int(w * 0.78), int(h * 0.52))
    cx = pad + w // 2
    if pic is not None:
        img.alpha_composite(pic, (cx - pic.width // 2, pad + int(24 * u)))
        y = pad + int(24 * u) + int(h * 0.52) + int(14 * u)
        y = draw_text(img, (cx, y), item["label"], "ExtraBold", int(44 * u), accent,
                      int(w * 0.88), 2)
        draw_text(img, (cx, y + int(6 * u)), item.get("detail", ""), "Regular",
                  int(30 * u), MUTED, int(w * 0.86), 3)
    else:
        y = pad + int(h * (0.18 if item.get("detail", "").strip() else 0.36))
        y = draw_text(img, (cx, y), item["label"], "ExtraBold", int(58 * u), accent,
                      int(w * 0.86), 3)
        draw_text(img, (cx, y + int(12 * u)), item.get("detail", ""), "Regular",
                  int(32 * u), MUTED, int(w * 0.86), 4)
    return pil_to_sprite(img)


def row_sprite(item: dict, w: int, h: int, accent: tuple, u: float) -> np.ndarray:
    """Horizontal list row: picture (or accent dot) left, label + detail right."""
    from PIL import ImageDraw
    img, pad = _card_base(w, h, accent, u)
    side = int(h * 0.80)
    x0 = pad + int(28 * u)
    pic = _picture(item.get("picture"), side, side)
    if pic is not None:
        img.alpha_composite(pic, (x0 + (side - pic.width) // 2,
                                  pad + (h - pic.height) // 2 + int(4 * u)))
    else:
        rr = int(h * 0.16)
        cy = pad + h // 2 + int(4 * u)
        ImageDraw.Draw(img).ellipse((x0 + side // 2 - rr, cy - rr, x0 + side // 2 + rr,
                                     cy + rr), fill=accent + (255,))
    tx = x0 + side + int(34 * u)
    has_detail = bool(item.get("detail", "").strip())
    y = pad + int(h * (0.20 if has_detail else 0.34))
    y = draw_text(img, (tx, y), item["label"], "ExtraBold", int(48 * u), INK,
                  w - (tx - pad) - int(30 * u), 1, anchor="la")
    draw_text(img, (tx, y + int(4 * u)), item.get("detail", ""), "Regular", int(32 * u),
              MUTED, w - (tx - pad) - int(30 * u), 2, anchor="la")
    return pil_to_sprite(img)


# ====================================================================== layout

def _background(w: int, h: int) -> np.ndarray:
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    col = np.array(BG_TOP, np.float32) * (1 - t) + np.array(BG_BOTTOM, np.float32) * t
    bg = np.ascontiguousarray(np.broadcast_to(col, (h, w, 3)).astype(np.uint8))
    import cv2
    step = int(46 * h / 1080)                     # faint dot grid, slide-deck feel
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            cv2.circle(bg, (x, y), max(1, step // 18), (214, 222, 236), -1, cv2.LINE_AA)
    return bg


class Slide:
    """Pre-built sprites + geometry for one slide; draw(frame, t) animates it."""

    def __init__(self, spec: dict, w: int, h: int, index: int):
        if spec["layout"] not in EXPLAIN_LAYOUTS:
            raise AnimateError(f"unknown slide layout '{spec['layout']}'")
        self.spec, self.w, self.h = spec, w, h
        self.u = u = h / 1080
        self.layout = spec["layout"]
        self.items = spec["items"]
        self.accent = ACCENTS[index % len(ACCENTS)]
        n = max(1, len(self.items))
        self.parts: list[tuple] = []          # (sprite, cx, cy, at, style)
        title = self.layout == "title"
        self.heading = text_sprite(spec["heading"], "ExtraBold",
                                   int((84 if title else 64) * u), INK)
        if self.heading.shape[1] > 0.88 * w:  # very long heading: shrink to fit
            k = 0.88 * w / self.heading.shape[1]
            self.heading = text_sprite(spec["heading"], "ExtraBold",
                                       int((84 if title else 64) * u * k), INK)
        self.head_xy = ((w / 2, 0.17 * h) if title else
                        (0.06 * w + self.heading.shape[1] / 2, 0.11 * h))

        if self.layout in ("title", "compare", "flow"):
            gap = (0.07 if self.layout == "flow" else 0.03) * w
            cw = int((0.88 * w - gap * (n - 1)) / n)
            ch = int((0.56 if title else 0.58 if self.layout == "compare" else 0.50) * h)
            if not any(it.get("picture") for it in self.items):
                ch = int(0.34 * h)                   # text-only cards: no empty white
            cy = (0.62 if title else 0.60) * h
            self.centres = []
            for k, it in enumerate(self.items):
                cx = 0.06 * w + cw / 2 + k * (cw + gap)
                acc = ACCENTS[(index + k) % len(ACCENTS)]
                self.parts.append((card_sprite(it, cw, ch, acc, u), cx, cy, it["at"], "pop"))
                self.centres.append((cx, cw))
            self.flow_y = cy
        elif self.layout == "list":
            gap = 0.025 * h
            rh = int(min(0.17 * h, (0.68 * h - gap * (n - 1)) / n))
            y0 = 0.24 * h + (0.68 * h - (rh * n + gap * (n - 1))) / 2
            for k, it in enumerate(self.items):
                acc = ACCENTS[(index + k) % len(ACCENTS)]
                spr = row_sprite(it, int(0.86 * w), rh, acc, u)
                self.parts.append((spr, 0.5 * w, y0 + rh / 2 + k * (rh + gap), it["at"], "slide"))
        elif self.layout == "big_number":
            it = self.items[0]
            pic_path = it.get("picture")
            self.num_x = 0.62 * w if pic_path else 0.5 * w
            if pic_path:
                pic = _picture(pic_path, int(0.36 * w), int(0.50 * h))
                if pic is not None:
                    self.parts.append((pil_to_sprite(pic), 0.25 * w, 0.58 * h, it["at"], "pop"))
            self.label = text_sprite(it["label"], "Bold", int(48 * u), MUTED)
            self.detail = text_sprite(it["detail"], "Regular", int(36 * u), MUTED) \
                if it.get("detail", "").strip() else None
        elif self.layout == "bars":
            self.max_v = max((abs(float(it["value"])) for it in self.items), default=1) or 1
            area = 0.72 * w
            self.bar_w = int(min(0.16 * w, area / n * 0.55))
            self.bar_x = [0.14 * w + area * (k + 0.5) / n for k in range(n)]
            self.base_y, self.bar_h = 0.80 * h, 0.46 * h
            self.bar_labels = [text_sprite(it["label"], "Bold", int(36 * u), INK)
                               for it in self.items]

    # ------------------------------------------------------------------ draw
    def draw(self, frame: np.ndarray, t: float, start: float) -> None:
        import cv2
        w, h, u = self.w, self.h, self.u
        # heading slides in from the left; accent underline draws under it
        p = ease_out_cubic((t - start - 0.1) / 0.5)
        hx, hy = self.head_xy
        blit(frame, self.heading, hx - (1 - p) * 0.25 * w, hy)
        if self.layout != "title":
            ln = int(0.10 * w * ease_out_cubic((t - start - 0.35) / 0.5))
            if ln > 0:
                y = int(hy + self.heading.shape[0] / 2 + 14 * u)
                x0 = int(0.06 * w)
                cv2.rectangle(frame, (x0, y), (x0 + ln, y + int(8 * u)), self.accent, -1)

        if self.layout == "flow":                  # arrows draw towards the next card
            for k in range(1, len(self.centres)):
                a = self.items[k]["at"]
                q = clamp01((t - (a - 0.35)) / 0.35)
                if q <= 0:
                    continue
                (x1, w1), (x2, w2) = self.centres[k - 1], self.centres[k]
                xa, xb = x1 + w1 / 2 + 10 * u, x2 - w2 / 2 - 10 * u
                xe = xa + (xb - xa) * q
                y = int(self.flow_y)
                cv2.arrowedLine(frame, (int(xa), y), (int(xe), y), MUTED, max(3, int(8 * u)),
                                cv2.LINE_AA, tipLength=min(0.6, 30 * u / max(1.0, xe - xa)))

        for k, (spr, cx, cy, at, style) in enumerate(self.parts):
            age = t - at
            if age < 0:
                continue
            float_y = math.sin(1.6 * t + k * 1.3) * 4 * u
            if style == "pop":
                s = ease_out_back(age / POP_S)
                blit(frame, transform(spr, 0.25 + 0.75 * s, 0.0), cx, cy + float_y)
            else:                                    # slide in from the left
                q = ease_out_cubic(age / POP_S)
                blit(frame, spr, cx - (1 - q) * 0.6 * w, cy)

        if self.layout == "big_number":
            it = self.items[0]
            age = t - it["at"]
            if age >= 0:
                v = float(it["value"]) * ease_out_cubic(age / 1.2)
                num = text_sprite(fmt_value(v, it["unit"], float(it["value"])), "Black",
                                  int(160 * u), self.accent)
                s = 0.6 + 0.4 * ease_out_back(age / POP_S)
                blit(frame, self.label, self.num_x, 0.36 * h)
                blit(frame, transform(num, s, 0.0), self.num_x, 0.55 * h)
                if self.detail is not None:
                    blit(frame, self.detail, self.num_x, 0.73 * h)
        elif self.layout == "bars":
            x0, x1 = int(0.10 * w), int(0.90 * w)
            cv2.rectangle(frame, (x0, int(self.base_y)), (x1, int(self.base_y + 4 * u)),
                          MUTED, -1)
            for k, it in enumerate(self.items):
                cx = self.bar_x[k]
                blit(frame, self.bar_labels[k], cx, self.base_y + 40 * u)
                age = t - it["at"]
                if age < 0:
                    continue
                g = ease_out_cubic(age / 0.8)
                bh = int(self.bar_h * abs(float(it["value"])) / self.max_v * g)
                acc = ACCENTS[k % len(ACCENTS)]
                if bh > 0:
                    cv2.rectangle(frame, (int(cx - self.bar_w / 2), int(self.base_y - bh)),
                                  (int(cx + self.bar_w / 2), int(self.base_y)), acc, -1)
                val = text_sprite(fmt_value(float(it["value"]) * g, it["unit"],
                                            float(it["value"])), "ExtraBold",
                                  int(44 * u), INK)
                blit(frame, val, cx, self.base_y - bh - 34 * u)


# ====================================================================== render

def render_track(slides: list[dict], bounds: list[float], w: int, h: int, fps: int,
                 out_path: str | Path, cfg: dict) -> Path:
    """Draw and encode the silent slide track (see module docstring)."""
    if w < h:
        raise AnimateError("slides.render_track is landscape-only for now")
    if len(bounds) != len(slides) + 1 or not slides:
        raise AnimateError(f"{len(slides)} slides need {len(slides) + 1} bounds")
    marks = frame_marks(bounds, fps)
    n = marks[-1]
    t0 = bounds[0]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg = _background(w, h)
    built: dict[int, Slide] = {}

    def slide(i: int) -> Slide:
        if i not in built:
            for old in [k for k in built if k < i - 1]:
                del built[old]
            built[i] = Slide(slides[i], w, h, i)
        return built[i]

    def draw(i: int, t: float) -> np.ndarray:
        frame = bg.copy()
        slide(i).draw(frame, t, bounds[i])
        return frame

    bar_h = max(4, int(8 * h / 1080))
    log.info("rendering %d slides, %d frames (%dx%d @ %dfps)", len(slides), n, w, h, fps)
    si = 0
    with RawVideoWriter(out_path, w, h, fps, video_encode_args(cfg)) as wr:
        for k in range(n):
            while k >= marks[si + 1]:
                si += 1
            t = t0 + k / fps
            frame = draw(si, t)
            age = t - bounds[si]
            if si > 0 and age < PUSH_S:             # push: old slide leaves left
                x = int(smoothstep(age / PUSH_S) * w)
                old = draw(si - 1, t)
                if x < w:
                    frame = np.concatenate([old[:, x:], frame[:, :x]], axis=1) if x > 0 else old
            frame[h - bar_h:, : int(w * (k + 1) / n)] = ACCENTS[0]   # progress bar
            wr.write(np.ascontiguousarray(frame))
    return out_path

