"""Cartoon animation renderer for Create mode (create.visual_style: animated).

Every frame is drawn in Python and streamed straight into the encoder
(ffutil.RawVideoWriter), so nothing but the finished silent track hits disk:

  background  a procedural scene per chapter (space, ocean, sky, meadow, snow,
              desert, body) with animated particles; cross-fades on change
  icons       Noto Emoji 512px PNGs (Apache-2.0, fetched once into
              cache/emoji): 1-3 per narrated line, bounce in, float, pop out
  mascot      an original owl drawn with Pillow - its beak follows the
              narration's loudness, it blinks, bobs while talking, waves
  banners     an animated title card on the intro + a banner per chapter

Captions are NOT drawn here: create.finish_video burns them with the shared
ASS pipeline. Sprites are kept premultiplied-alpha so scaling/rotating never
leaves dark fringes.

Illustrated style (create.visual_style: illustrated): when scenes carry "art",
the procedural background and the emoji are replaced by AI pictures (art.py):
ArtShot puts the painted-out background under an eased camera move (zoom in /
out, pan) and the cut-out character on top with parallax, bob, sway and a
pop-in; consecutive pictures cross-fade. Mascot and banners stay.

Public API:
  render_track(scenes, bounds, w, h, fps, voice_wav, out_path, cfg, seed) -> Path
    scenes[i] = {"scene": str, "icons": [emoji, ...],
                 "banner": None | {"kind": "title"|"chapter", "text", "label"},
                 "art": {"shot": int, "bg": png, "fg": png|None, "box": [x,y,w,h]|None}
                        (optional; all scenes or none)}
    bounds    = len(scenes)+1 absolute times (seconds) - scene i spans
                [bounds[i], bounds[i+1]); frame k is at bounds[0] + k/fps.
"""
from __future__ import annotations

import math
import random
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from config import ROOT
from errors import ClipForgeError
from ffutil import RawVideoWriter, video_encode_args
from logutil import get_logger
from schemas import KIDS_SCENES

log = get_logger("animate")

THEMES = KIDS_SCENES
MOUTH_LEVELS = 5                 # beak openness 0 (closed) .. 5 (wide)
_USER_AGENT = "ClipForge/2.0 (+https://github.com/; emoji fetch)"
_FONT = ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"


class AnimateError(ClipForgeError):
    stage = "animate"


# ====================================================================== maths

def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out_back(x: float) -> float:
    """0→1 with a springy overshoot (peaks ~1.1) - the 'pop' in pop-in."""
    x = clamp01(x)
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def ease_out_cubic(x: float) -> float:
    return 1 - (1 - clamp01(x)) ** 3


def ease_in_cubic(x: float) -> float:
    return clamp01(x) ** 3


def frame_marks(bounds: list[float], fps: float) -> list[int]:
    """Cumulative frame index of every boundary (no per-scene rounding drift)."""
    if len(bounds) < 2:
        raise AnimateError("need at least two scene boundaries")
    marks = [round((b - bounds[0]) * fps) for b in bounds]
    if any(b <= a for a, b in zip(marks, marks[1:])):
        raise AnimateError("a scene rounds to zero frames")
    return marks


def layout(w: int, h: int) -> dict:
    """Where things go. Landscape: mascot bottom-left, icons centre-right above
    the bottom captions. Portrait (Shorts): icons on top, captions at ~0.60h
    (create.shorts.caption_anchor), mascot standing at the bottom."""
    if w >= h:
        return {"icons": (0.30 * w, 0.17 * h, 0.97 * w, 0.70 * h),
                "mascot_h": 0.50 * h, "mascot_x": 0.155 * w,
                "mascot_bottom": 1.0 * h,
                "banner_y": 0.035 * h, "banner_h": 0.13 * h, "banner_w": 0.62 * w,
                "title_y": 0.08 * h, "title_h": 0.24 * h, "title_w": 0.66 * w}
    return {"icons": (0.06 * w, 0.14 * h, 0.94 * w, 0.50 * h),
            "mascot_h": 0.27 * h, "mascot_x": 0.5 * w, "mascot_bottom": 1.0 * h,
            "banner_y": 0.035 * h, "banner_h": 0.075 * h, "banner_w": 0.90 * w,
            "title_y": 0.05 * h, "title_h": 0.12 * h, "title_w": 0.92 * w}


def icon_slots(n: int, region: tuple) -> list[tuple[float, float, int]]:
    """(cx, cy, size_px) for 1-3 icons inside region (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = region
    rw, rh = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if n <= 1:
        return [(cx, cy, int(min(rw, rh) * 0.80))]
    if n == 2:
        s = int(min(rw / 2, rh) * 0.78)
        return [(x0 + rw * 0.27, cy, s), (x0 + rw * 0.73, cy, s)]
    if rw / rh >= 1.6:                                   # wide: one row
        s = int(min(rw / 3, rh) * 0.80)
        return [(x0 + rw * f, cy, s) for f in (0.18, 0.50, 0.82)]
    s = int(min(rw / 2, rh / 2) * 0.86)                  # tall: triangle
    return [(cx, y0 + rh * 0.27, s), (x0 + rw * 0.28, y0 + rh * 0.74, s),
            (x0 + rw * 0.72, y0 + rh * 0.74, s)]


def mouth_levels(samples: np.ndarray, sr: int, times: np.ndarray, fps: float,
                 levels: int = MOUTH_LEVELS) -> np.ndarray:
    """Beak openness per frame (ints 0..levels) from the narration loudness.
    RMS over one frame window, normalised by the 90th percentile of voiced
    frames, fast attack / slower release so the beak doesn't chatter."""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    half = max(1, int(sr / fps / 2))
    centre = np.round(np.asarray(times, dtype=np.float64) * sr).astype(np.int64)
    a = np.clip(centre - half, 0, len(x))
    b = np.clip(centre + half, 0, len(x))
    rms = np.sqrt(np.maximum(cs[b] - cs[a], 0.0) / np.maximum(b - a, 1))
    voiced = rms[rms > 1e-3]
    norm = float(np.percentile(voiced, 90)) if voiced.size else 1.0
    env = np.zeros_like(rms)
    prev = 0.0
    for i, r in enumerate(rms / max(norm, 1e-6)):
        prev = r if r > prev else prev * 0.55
        env[i] = prev
    lv = np.round(np.clip(env, 0.0, 1.0) * levels).astype(np.int64)
    lv[env < 0.12] = 0
    return lv


def blink_states(n: int, fps: float, seed: int) -> np.ndarray:
    """Eye state per frame: 0 open, 1 half, 2 closed. A blink every 2-5 s."""
    eyes = np.zeros(n, dtype=np.int64)
    rng = random.Random(seed)
    k = int(rng.uniform(1.0, 3.0) * fps)
    while k < n:
        for off, state in enumerate((1, 2, 2, 1)):
            if k + off < n:
                eyes[k + off] = state
        k += int(rng.uniform(2.0, 5.0) * fps)
    return eyes


# ====================================================================== sprites

def premultiply(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.uint16)
    rgb = (rgba[..., :3].astype(np.uint16) * a + 127) // 255
    return np.concatenate([rgb.astype(np.uint8), rgba[..., 3:4]], axis=2)


def pil_to_sprite(img) -> np.ndarray:
    return premultiply(np.asarray(img.convert("RGBA"), dtype=np.uint8))


def blit(dst: np.ndarray, spr: np.ndarray | None, cx: float, cy: float) -> None:
    """Composite premultiplied RGBA `spr` centred at (cx, cy), clipped."""
    if spr is None:
        return
    h, w = spr.shape[:2]
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    dx0, dy0 = max(0, x0), max(0, y0)
    dx1, dy1 = min(dst.shape[1], x0 + w), min(dst.shape[0], y0 + h)
    if dx1 <= dx0 or dy1 <= dy0:
        return
    import cv2
    s = spr[dy0 - y0:dy1 - y0, dx0 - x0:dx1 - x0]
    d = dst[dy0:dy1, dx0:dx1]
    # premultiplied "over": out = src_rgb + dst * (255 - a) / 255, via OpenCV's
    # saturating SIMD ops (~5x faster than the equivalent uint16 numpy maths)
    c0, c1, c2, a = cv2.split(s)
    inv = cv2.bitwise_not(a)
    kept = cv2.multiply(d, cv2.merge((inv, inv, inv)), scale=1 / 255)
    d[...] = cv2.add(kept, cv2.merge((c0, c1, c2)))


def over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Premultiplied RGBA `src` composited over RGBA `dst` (both centred; src
    no larger than dst). Returns a new array. For one-off sprite building."""
    out = dst.copy()
    h, w = src.shape[:2]
    y0, x0 = (dst.shape[0] - h) // 2, (dst.shape[1] - w) // 2
    region = out[y0:y0 + h, x0:x0 + w].astype(np.uint16)
    inv = 255 - src[..., 3:4].astype(np.uint16)
    region = src.astype(np.uint16) + (region * inv + 127) // 255
    out[y0:y0 + h, x0:x0 + w] = np.minimum(region, 255).astype(np.uint8)
    return out


def transform(spr: np.ndarray, scale: float, angle: float) -> np.ndarray | None:
    """Scale + rotate (degrees, counter-clockwise) about the sprite centre."""
    import cv2
    if scale <= 0.02:
        return None
    if abs(angle) < 0.05 and abs(scale - 1.0) < 0.004:
        return spr
    h, w = spr.shape[:2]
    rad = math.radians(angle)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    nw = int(math.ceil((w * c + h * s) * scale)) + 2
    nh = int(math.ceil((w * s + h * c) * scale)) + 2
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(spr, m, (nw, nh), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


def glow_sprite(size: int) -> np.ndarray:
    """Soft white radial glow that sits behind an icon so it pops off any
    background."""
    r = np.linspace(-1, 1, size, dtype=np.float32)
    d = np.sqrt(r[None, :] ** 2 + r[:, None] ** 2)
    a = (np.clip(1 - d, 0, 1) ** 1.6 * 150).astype(np.uint8)
    return np.dstack([a, a, a, a])        # premultiplied white


# ====================================================================== emoji

def resolve_emoji(token: str) -> str | None:
    """An emoji character as given, or an emoji *name* ('octopus') looked up
    in the Unicode database. None for empty/unknown text."""
    s = str(token or "").strip()
    if not s:
        return None
    if any(ord(ch) > 0x7F for ch in s):
        return s
    try:
        return unicodedata.lookup(s.replace("_", " ").upper())
    except KeyError:
        return None


def emoji_filename(emoji: str) -> str | None:
    """Noto's file name for an emoji: emoji_u1f419.png, ZWJ parts joined by
    '_', variation selector FE0F dropped, each code point padded to 4 hex."""
    cps = [f"{ord(ch):04x}" for ch in emoji if ord(ch) != 0xFE0F]
    return f"emoji_u{'_'.join(cps)}.png" if cps else None


_icons: dict[str, np.ndarray | None] = {}
_OFFLINE_BACKOFF_S = 600
_offline_until = 0.0     # after a network failure, skip downloads until then


def icon_sprite(token: str, cfg: dict) -> np.ndarray | None:
    """512px premultiplied sprite for one emoji, downloaded once into
    cache/emoji. None (logged) when it can't be resolved or fetched: a
    missing icon must never fail a render. After a network failure, new
    downloads are skipped for _OFFLINE_BACKOFF_S so a dropped connection costs
    one timeout, not one per scene."""
    global _offline_until
    from PIL import Image
    emoji = resolve_emoji(token)
    name = emoji_filename(emoji) if emoji else None
    if not name:
        log.warning("icon %r is not an emoji, skipped", token)
        return None
    if name in _icons:
        return _icons[name]
    path = ROOT / cfg["paths"]["cache_dir"] / "emoji" / name
    if not path.is_file():
        if time.monotonic() < _offline_until:
            return None
        url = str(cfg["create"]["animation"]["emoji_url"]).format(name=name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                f.write(resp.read())
            tmp.replace(path)
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            log.warning("no Noto image for %r (%s, HTTP %s)", token, name, e.code)
            if e.code == 404:
                _icons[name] = None       # permanent: don't ask again this run
            return None
        except (OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            _offline_until = time.monotonic() + _OFFLINE_BACKOFF_S
            log.warning("emoji download failed for %r (%s); skipping new emoji "
                        "downloads for %ds", token, e, _OFFLINE_BACKOFF_S)
            return None
    try:
        with Image.open(path) as img:
            spr = pil_to_sprite(img)
    except (OSError, ValueError) as e:
        path.unlink(missing_ok=True)
        log.warning("corrupt emoji file %s removed (%s)", path.name, e)
        return None
    _icons[name] = spr
    return spr


# ====================================================================== mascot

class Mascot:
    """'Ollie' the owl. Every (beak, eyes, wing) pose is drawn once with Pillow
    at 2x and downsampled to the target height; frames just pick a pose."""

    W, H, OX = 740, 660, 110           # design canvas; body art spans x 0..520, shifted OX right so raised wings fit
    BODY, BELLY, FACE = (150, 100, 62), (246, 223, 186), (198, 142, 96)
    WING, OUT, BEAK = (118, 76, 46), (62, 40, 28), (250, 176, 58)
    BEAK_LOW, MOUTH, TONGUE = (226, 138, 38), (112, 30, 42), (238, 112, 122)
    FEET, GLASS, CHEEK = (247, 166, 60), (58, 44, 110), (240, 140, 140, 150)

    S = 2                              # supersampling factor for drawing

    def __init__(self, height: int):
        from PIL import Image
        self.height = max(40, int(height))
        self.width = int(round(self.W * self.height / self.H))
        # wings only depend on the wing pose: rotate them 3x, not once per pose
        wings = {pose: self._wings(pose) for pose in (0, 1, 2)}
        self.poses: dict[tuple[int, int, int], np.ndarray] = {}
        for mouth in range(MOUTH_LEVELS + 1):
            for eye in (0, 1, 2):
                for wing in (0, 1, 2):
                    img = self._draw(mouth / MOUTH_LEVELS, eye, wings[wing])
                    img = img.resize((self.width, self.height), Image.LANCZOS)
                    self.poses[(mouth, eye, wing)] = pil_to_sprite(img)

    def sc(self, *v):   # design coords (x, y, x, y, ...) -> canvas pixels
        return tuple(int(round((x + (self.OX if i % 2 == 0 else 0)) * self.S))
                     for i, x in enumerate(v))

    def _wings(self, pose: int):
        """Both wings, swung OUTWARD about the shoulders, on a transparent
        canvas-sized layer (PIL rotate: + is counter-clockwise, so the left
        wing turns clockwise)."""
        from PIL import Image, ImageDraw
        s, sc = self.S, self.sc
        out = Image.new("RGBA", (self.W * s, self.H * s), (0, 0, 0, 0))
        angle = (8, 32, 76)[pose]
        for side in (-1, 1):
            layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            px = 260 + side * 150
            ld.ellipse(sc(px - 40, 340, px + 40, 545), fill=self.WING,
                       outline=self.OUT, width=7 * s)
            for k in range(3):
                y = 410 + k * 38
                ld.arc(sc(px - 24, y, px + 24, y + 36), 20, 160,
                       fill=self.OUT, width=4 * s)
            out.alpha_composite(layer.rotate(side * angle, resample=Image.BICUBIC,
                                             center=sc(px, 352)))
        return out

    def _draw(self, mouth: float, eye: int, wing_layer):
        from PIL import Image, ImageDraw
        s, sc = self.S, self.sc

        def ss(*v):   # sizes / coords local to a sub-image: scale only
            return tuple(int(round(x * s)) for x in v)

        img = Image.new("RGBA", (self.W * s, self.H * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        lw = 7 * s
        # feet
        for fx in (190, 330):
            d.ellipse(sc(fx - 46, 606, fx + 46, 648), fill=self.FEET,
                      outline=self.OUT, width=6 * s)
        # ear tufts, then the body over their bases
        d.polygon([sc(126, 238), sc(138, 116), sc(220, 192)], fill=self.BODY,
                  outline=self.OUT, width=lw)
        d.polygon([sc(394, 238), sc(382, 116), sc(300, 192)], fill=self.BODY,
                  outline=self.OUT, width=lw)
        d.ellipse(sc(92, 150, 428, 642), fill=self.BODY, outline=self.OUT, width=lw)
        img.alpha_composite(wing_layer)          # wings over the body's sides
        d = ImageDraw.Draw(img)
        # belly with feather chevrons
        d.ellipse(sc(158, 360, 362, 618), fill=self.BELLY)
        for row, y in enumerate((420, 468, 516, 562)):
            xs = (214, 260, 306) if row % 2 == 0 else (237, 283)
            for x in xs:
                d.arc(sc(x - 18, y - 14, x + 18, y + 14), 20, 160,
                      fill=(214, 182, 138), width=5 * s)
        # facial discs
        d.ellipse(sc(118, 182, 278, 350), fill=self.FACE)
        d.ellipse(sc(242, 182, 402, 350), fill=self.FACE)
        # cheeks (translucent)
        cheeks = Image.new("RGBA", img.size, (0, 0, 0, 0))
        cd = ImageDraw.Draw(cheeks)
        cd.ellipse(sc(140, 318, 188, 342), fill=self.CHEEK)
        cd.ellipse(sc(332, 318, 380, 342), fill=self.CHEEK)
        img.alpha_composite(cheeks)
        d = ImageDraw.Draw(img)
        # eyes (circular layer so the lid stays inside the eye)
        r = 56
        for ex, look in ((196, 7), (324, -7)):
            eye_img = Image.new("RGBA", ss(2 * r, 2 * r), (0, 0, 0, 0))
            ed = ImageDraw.Draw(eye_img)
            ed.ellipse(ss(0, 0, 2 * r, 2 * r), fill=(255, 255, 255))
            px, py = r + look, r + 6
            ed.ellipse(ss(px - 27, py - 27, px + 27, py + 27), fill=(34, 28, 40))
            ed.ellipse(ss(px - 18, py - 20, px - 2, py - 4), fill=(255, 255, 255))
            ed.ellipse(ss(px + 6, py + 6, px + 13, py + 13), fill=(255, 255, 255))
            if eye:
                cover = 0.55 if eye == 1 else 1.0
                ed.rectangle(ss(0, 0, 2 * r, 2 * r * cover), fill=self.FACE)
                if eye == 2:
                    ed.arc(ss(16, r - 10, 2 * r - 16, r + 24), 10, 170,
                           fill=self.OUT, width=6 * s)
            mask = Image.new("L", eye_img.size, 0)
            ImageDraw.Draw(mask).ellipse(ss(0, 0, 2 * r, 2 * r), fill=255)
            eye_img.putalpha(mask)
            img.alpha_composite(eye_img, dest=sc(ex - r, 265 - r))
            d = ImageDraw.Draw(img)
            d.ellipse(sc(ex - r, 265 - r, ex + r, 265 + r), outline=self.OUT,
                      width=6 * s)
        # glasses
        for ex in (196, 324):
            d.ellipse(sc(ex - 66, 199, ex + 66, 331), outline=self.GLASS,
                      width=7 * s)
        d.arc(sc(246, 240, 274, 268), 200, 340, fill=self.GLASS, width=7 * s)
        # beak: mouth interior, lower beak (drops with `mouth`), upper beak
        o = clamp01(mouth)
        if o > 0.05:
            d.ellipse(sc(240, 318, 280, 334 + o * 40), fill=self.MOUTH)
            if o > 0.4:
                d.ellipse(sc(249, 322 + o * 22, 271, 332 + o * 38), fill=self.TONGUE)
        d.polygon([sc(243, 326 + o * 30), sc(277, 326 + o * 30),
                   sc(260, 350 + o * 34)], fill=self.BEAK_LOW, outline=self.OUT,
                  width=4 * s)
        d.polygon([sc(260, 294), sc(236, 324), sc(260, 340), sc(284, 324)],
                  fill=self.BEAK, outline=self.OUT, width=4 * s)
        return img


_mascots: dict[int, Mascot] = {}


def get_mascot(height: int) -> Mascot:
    """Build each mascot size once per process (a few seconds of drawing) -
    the long video and every Short of a run reuse it."""
    h = max(40, int(height))
    if h not in _mascots:
        _mascots[h] = Mascot(h)
    return _mascots[h]


# ====================================================================== banners

_PALETTE = ((255, 92, 98), (58, 156, 255), (255, 178, 36), (76, 196, 118),
            (166, 108, 255), (255, 120, 196))


def banner_text(s: str | None) -> str:
    """Pure: keep only characters the bundled Montserrat can draw (Latin,
    Greek/Cyrillic and common punctuation/currency, all below U+2100); emoji
    and other symbols in LLM-written titles would otherwise render as empty
    boxes."""
    kept = "".join(ch for ch in str(s or "") if ord(ch) < 0x2100)
    return " ".join(kept.split())


def banner_sprite(text: str, label: str | None, max_w: int, height: int,
                  fill: tuple) -> np.ndarray:
    """Pill-shaped banner: optional small label line above up to two lines of
    big outlined text. Shrinks the font until it fits `max_w`."""
    from PIL import Image, ImageDraw, ImageFont
    if not _FONT.is_file():
        raise AnimateError(f"banner font missing: {_FONT}")
    text, label = banner_text(text), (banner_text(label) or None)
    pad = int(height * 0.32)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def width_of(t, f, sw):
        b = probe.textbbox((0, 0), t, font=f, stroke_width=sw)
        return b[2] - b[0], b[3] - b[1]

    words = str(text).split()
    best = None
    size = int(height * 0.46)
    while size >= max(14, int(height * 0.18)) and best is None:
        font = ImageFont.truetype(str(_FONT), size)
        sw = max(2, size // 12)
        candidates = [[" ".join(words)]]
        if len(words) > 1:
            candidates += [[" ".join(words[:i]), " ".join(words[i:])]
                           for i in range(1, len(words))]
        for lines in candidates:
            if max(width_of(ln, font, sw)[0] for ln in lines) + 2 * pad <= max_w:
                if best is None or len(lines) < len(best[0]):
                    best = (lines, font, sw)
                    break
        size -= 2
    if best is None:
        font = ImageFont.truetype(str(_FONT), max(14, int(height * 0.18)))
        best = ([" ".join(words)], font, 2)
    lines, font, sw = best
    lab_font = ImageFont.truetype(str(_FONT), max(12, int(height * 0.20)))
    line_h = width_of("Ag", font, sw)[1]
    lab_h = width_of("Ag", lab_font, 2)[1] if label else 0
    gap = int(line_h * 0.18)
    inner_h = lab_h + (gap if label else 0) + len(lines) * line_h + (len(lines) - 1) * gap
    bh = max(height, inner_h + pad)
    bw = min(max_w, max(width_of(ln, font, sw)[0] for ln in lines) + 2 * pad)
    shadow = max(4, bh // 14)
    img = Image.new("RGBA", (bw + shadow, bh + shadow), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = min(bh // 2, int(height * 0.5))
    d.rounded_rectangle((shadow, shadow, bw + shadow - 1, bh + shadow - 1), rad,
                        fill=(30, 20, 50, 110))
    d.rounded_rectangle((0, 0, bw - 1, bh - 1), rad, fill=fill,
                        outline=(255, 255, 255), width=max(3, bh // 18))
    y = (bh - inner_h) // 2
    if label:
        lw_, _ = width_of(label, lab_font, 2)
        d.text(((bw - lw_) // 2, y), label, font=lab_font, fill=(255, 255, 255),
               stroke_width=2, stroke_fill=(40, 28, 64))
        y += lab_h + gap
    for ln in lines:
        tw, _ = width_of(ln, font, sw)
        d.text(((bw - tw) // 2, y - int(line_h * 0.12)), ln, font=font,
               fill=(255, 255, 255), stroke_width=sw, stroke_fill=(40, 28, 64))
        y += line_h + gap
    return pil_to_sprite(img)


# ====================================================================== backgrounds

def _vgrad(w: int, h: int, top: tuple, bottom: tuple) -> np.ndarray:
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    col = np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t
    return np.ascontiguousarray(np.broadcast_to(col, (h, w, 3)).astype(np.uint8))


def _wave(img, w, h, base, amp, period, phase, color) -> None:
    import cv2
    xs = np.linspace(0, w, 96)
    ys = base * h + amp * h * np.sin(2 * np.pi * xs / (period * w) + phase)
    pts = np.concatenate([np.stack([xs, ys], 1), [[w, h + 2], [0, h + 2]]])
    cv2.fillPoly(img, [pts.astype(np.int32)], color, cv2.LINE_AA)


def _cloud(img, cx, cy, s, color=(255, 255, 255)) -> None:
    import cv2
    for dx, dy, r in ((0, 0, 1.0), (-0.95, 0.28, 0.68), (0.95, 0.3, 0.72),
                      (-0.45, -0.38, 0.74), (0.5, -0.3, 0.66)):
        cv2.circle(img, (int(cx + dx * s), int(cy + dy * s)), int(r * s), color,
                   -1, cv2.LINE_AA)


class Background:
    """One animated scene. The static layer is built once; frame(t) copies it
    and draws the moving bits as pure functions of t (no per-frame state, so
    long video and Shorts stay in sync at the same timestamp)."""

    def __init__(self, theme: str, w: int, h: int, seed: int = 0):
        self.theme = theme if theme in THEMES else "sky"
        self.w, self.h, self.u = w, h, min(w, h)
        self.rng = np.random.default_rng(seed * 7919 + THEMES.index(self.theme))
        self.base = getattr(self, f"_base_{self.theme}")()

    def frame(self, t: float) -> np.ndarray:
        img = self.base.copy()
        getattr(self, f"_anim_{self.theme}")(img, t)
        return img

    def _pts(self, n):
        r = self.rng
        return (r.uniform(0, self.w, n), r.uniform(0, self.h, n),
                r.uniform(0, 2 * np.pi, n), r.uniform(0.5, 1.5, n))

    def _sky_base(self, top, bottom, sun=True):
        import cv2
        img = _vgrad(self.w, self.h, top, bottom)
        if sun:
            c = (int(0.86 * self.w), int(0.15 * self.h))
            for k, a in ((2.0, 0.18), (1.5, 0.25)):
                glow = img.copy()
                cv2.circle(glow, c, int(0.07 * self.u * k), (255, 240, 170), -1,
                           cv2.LINE_AA)
                cv2.addWeighted(glow, a, img, 1 - a, 0, img)
            cv2.circle(img, c, int(0.07 * self.u), (255, 214, 80), -1, cv2.LINE_AA)
        self.clouds = (self.rng.uniform(0, 1.5, 7), self.rng.uniform(0.07, 0.42, 7),
                       self.rng.uniform(0.05, 0.085, 7), self.rng.uniform(0.006, 0.016, 7))
        return img

    def _draw_clouds(self, img, t, count=7):
        x0, y, s, v = self.clouds
        span = 1.5 * self.w
        for i in range(count):
            x = ((x0[i] * self.w + v[i] * self.w * t) % span) - 0.25 * self.w
            _cloud(img, x, y[i] * self.h, s[i] * self.u)

    # --- space
    def _base_space(self):
        import cv2
        img = _vgrad(self.w, self.h, (10, 8, 36), (44, 20, 82))
        xs, ys, _, b = self._pts(260)
        for x, y, br in zip(xs, ys, b):
            v = int(110 + 90 * br)
            cv2.circle(img, (int(x), int(y)), max(1, int(self.u / 900)),
                       (v, v, min(255, v + 20)), -1, cv2.LINE_AA)
        c, r = (int(0.89 * self.w), int(0.86 * self.h)), int(0.13 * self.u)
        cv2.ellipse(img, c, (int(r * 1.9), int(r * 0.42)), -12, 180, 360,
                    (210, 170, 240), max(2, r // 9), cv2.LINE_AA)
        cv2.circle(img, c, r, (150, 92, 200), -1, cv2.LINE_AA)
        cv2.ellipse(img, (c[0], c[1] - r // 3), (int(r * 0.92), r // 7), -12, 0,
                    360, (176, 118, 222), -1, cv2.LINE_AA)
        cv2.ellipse(img, c, (int(r * 1.9), int(r * 0.42)), -12, 0, 180,
                    (210, 170, 240), max(2, r // 9), cv2.LINE_AA)
        cv2.circle(img, (int(0.08 * self.w), int(0.12 * self.h)), int(0.035 * self.u),
                   (205, 205, 220), -1, cv2.LINE_AA)
        self.tw = self._pts(40)
        return img

    def _anim_space(self, img, t):
        import cv2
        xs, ys, ph, f = self.tw
        for x, y, p, fr in zip(xs, ys, ph, f):
            b = 0.55 + 0.45 * math.sin(2 * math.pi * 0.5 * fr * t + p)
            col = (int(255 * b), int(245 * b), int(190 * b))
            r = max(2, int(self.u / 360 * fr))
            cv2.circle(img, (int(x), int(y)), r, col, -1, cv2.LINE_AA)
            if fr > 1.25:
                L = int(r * 3 * b)
                cv2.line(img, (int(x) - L, int(y)), (int(x) + L, int(y)), col, 1, cv2.LINE_AA)
                cv2.line(img, (int(x), int(y) - L), (int(x), int(y) + L), col, 1, cv2.LINE_AA)

    # --- ocean
    def _base_ocean(self):
        import cv2
        img = _vgrad(self.w, self.h, (112, 206, 236), (8, 62, 122))
        rays = img.copy()
        for x in np.linspace(0.05, 0.95, 6) * self.w:
            pts = np.array([[x - 0.02 * self.w, 0], [x + 0.03 * self.w, 0],
                            [x + 0.12 * self.w, self.h], [x - 0.06 * self.w, self.h]],
                           np.int32)
            cv2.fillPoly(rays, [pts], (235, 250, 255), cv2.LINE_AA)
        cv2.addWeighted(rays, 0.09, img, 0.91, 0, img)
        _wave(img, self.w, self.h, 0.91, 0.012, 0.7, 0.4, (232, 206, 150))
        for x in self.rng.uniform(0.35, 0.98, 6) * self.w:
            ys = np.linspace(0.92 * self.h, self.rng.uniform(0.55, 0.75) * self.h, 18)
            xs = x + 0.012 * self.w * np.sin(np.linspace(0, 3 * np.pi, 18))
            cv2.polylines(img, [np.stack([xs, ys], 1).astype(np.int32)], False,
                          (46, 150, 92), max(4, int(self.u / 90)), cv2.LINE_AA)
        self.bub = self._pts(34)
        return img

    def _anim_ocean(self, img, t):
        import cv2
        xs, ys, ph, f = self.bub
        span = self.h * 1.1
        for x, y, p, fr in zip(xs, ys, ph, f):
            r = int(self.u * 0.008 * fr) + 2
            yy = self.h * 1.05 - ((y + 0.07 * self.h * fr * t) % span)
            xx = x + 0.01 * self.w * math.sin(1.6 * t + p)
            cv2.circle(img, (int(xx), int(yy)), r, (226, 244, 255),
                       max(1, r // 5), cv2.LINE_AA)
            cv2.circle(img, (int(xx - r * 0.35), int(yy - r * 0.35)), max(1, r // 4),
                       (255, 255, 255), -1, cv2.LINE_AA)

    # --- sky
    def _base_sky(self):
        return self._sky_base((96, 176, 250), (204, 234, 255))

    def _anim_sky(self, img, t):
        self._draw_clouds(img, t)

    # --- meadow
    def _base_meadow(self):
        import cv2
        img = self._sky_base((110, 190, 250), (214, 240, 255))
        _wave(img, self.w, self.h, 0.62, 0.03, 0.9, 1.0, (132, 206, 112))
        _wave(img, self.w, self.h, 0.76, 0.025, 0.6, 2.2, (84, 174, 74))
        cols = ((255, 110, 132), (255, 220, 80), (255, 255, 255), (184, 124, 255))
        for i in range(46):
            x = self.rng.uniform(0, self.w)
            y = self.rng.uniform(0.82, 0.98) * self.h
            cv2.circle(img, (int(x), int(y)), max(2, int(self.u / 170)),
                       cols[i % 4], -1, cv2.LINE_AA)
        return img

    def _anim_meadow(self, img, t):
        self._draw_clouds(img, t, count=4)

    # --- snow
    def _base_snow(self):
        img = _vgrad(self.w, self.h, (150, 190, 236), (222, 236, 250))
        _wave(img, self.w, self.h, 0.66, 0.03, 1.1, 0.3, (222, 234, 248))
        _wave(img, self.w, self.h, 0.77, 0.022, 0.7, 1.9, (246, 250, 255))
        self.flakes = self._pts(90)
        return img

    def _anim_snow(self, img, t):
        import cv2
        xs, ys, ph, f = self.flakes
        for x, y, p, fr in zip(xs, ys, ph, f):
            yy = ((y + 0.06 * self.h * fr * t) % (self.h + 20)) - 10
            xx = x + 0.012 * self.w * math.sin(0.9 * t + p)
            cv2.circle(img, (int(xx), int(yy)), max(2, int(self.u / 300 * fr)),
                       (255, 255, 255), -1, cv2.LINE_AA)

    # --- desert
    def _base_desert(self):
        import cv2
        img = _vgrad(self.w, self.h, (255, 192, 112), (255, 234, 172))
        c = (int(0.80 * self.w), int(0.22 * self.h))
        glow = img.copy()
        cv2.circle(glow, c, int(0.16 * self.u), (255, 248, 210), -1, cv2.LINE_AA)
        cv2.addWeighted(glow, 0.35, img, 0.65, 0, img)
        cv2.circle(img, c, int(0.09 * self.u), (255, 244, 196), -1, cv2.LINE_AA)
        _wave(img, self.w, self.h, 0.66, 0.04, 1.1, 0.8, (236, 186, 106))
        _wave(img, self.w, self.h, 0.79, 0.03, 0.7, 2.6, (220, 160, 80))
        self.clouds = (self.rng.uniform(0, 1.5, 2), self.rng.uniform(0.08, 0.3, 2),
                       self.rng.uniform(0.04, 0.06, 2), self.rng.uniform(0.004, 0.008, 2))
        return img

    def _anim_desert(self, img, t):
        self._draw_clouds(img, t, count=2)

    # --- body
    def _base_body(self):
        import cv2
        img = _vgrad(self.w, self.h, (255, 212, 216), (214, 86, 106))
        soft = img.copy()
        for _ in range(9):
            cv2.circle(soft, (int(self.rng.uniform(0, self.w)), int(self.rng.uniform(0, self.h))),
                       int(self.rng.uniform(0.08, 0.18) * self.u), (255, 236, 238), -1,
                       cv2.LINE_AA)
        cv2.addWeighted(soft, 0.22, img, 0.78, 0, img)
        self.cells = self._pts(16)
        self.white = self._pts(5)
        return img

    def _anim_body(self, img, t):
        import cv2
        span = 1.2 * self.w
        xs, ys, ph, f = self.cells
        ax = (int(0.045 * self.u), int(0.03 * self.u))
        for x, y, p, fr in zip(xs, ys, ph, f):
            xx = ((x + 0.03 * self.w * fr * t) % span) - 0.1 * self.w
            yy = y + 0.02 * self.h * math.sin(0.6 * t + p)
            ang = math.degrees(p) + 25 * t * fr
            cv2.ellipse(img, (int(xx), int(yy)), ax, ang, 0, 360, (204, 44, 58), -1,
                        cv2.LINE_AA)
            cv2.ellipse(img, (int(xx), int(yy)), (ax[0] // 2, ax[1] // 2), ang, 0, 360,
                        (172, 30, 46), -1, cv2.LINE_AA)
        xs, ys, ph, f = self.white
        for x, y, p, fr in zip(xs, ys, ph, f):
            xx = ((x + 0.015 * self.w * fr * t) % span) - 0.1 * self.w
            yy = y + 0.015 * self.h * math.sin(0.5 * t + p)
            r = int(0.035 * self.u)
            cv2.circle(img, (int(xx), int(yy)), r, (250, 242, 246), -1, cv2.LINE_AA)
            cv2.circle(img, (int(xx + r * 0.2), int(yy)), r // 2, (176, 120, 196), -1,
                       cv2.LINE_AA)


# ====================================================================== illustrations

CAMERA_MOVES = ("zoom_in", "pan_right", "zoom_out", "pan_left")
CAMERA_ZOOM = 0.10               # widest push-in over the cover fit (10%)


def smoothstep(x: float) -> float:
    x = clamp01(x)
    return x * x * (3 - 2 * x)


def camera(move: str, p: float, zoom: float = CAMERA_ZOOM) -> tuple[float, float, float]:
    """Pure. Camera at shot progress p (0..1) -> (z, fx, fy): z = zoom over the
    cover fit (1..1+zoom), (fx, fy) = window centre as a fraction of the free
    travel on each axis (-1..1, 0 = centred). Eased so shots start and end
    calm and cross-fades never jerk."""
    e = smoothstep(p)
    if move == "zoom_in":
        return 1 + zoom * e, 0.0, -0.4 * e
    if move == "zoom_out":
        return 1 + zoom * (1 - e), 0.0, -0.4 * (1 - e)
    if move == "pan_right":
        return 1 + zoom * 0.6, -0.8 + 1.6 * e, 0.0
    if move == "pan_left":
        return 1 + zoom * 0.6, 0.8 - 1.6 * e, 0.0
    raise AnimateError(f"unknown camera move '{move}'")


class ArtShot:
    """One illustration on screen (create.visual_style: illustrated).

    The painted-out background sits under a slow camera move; the cut-out
    character (art.cutout) rides on top with parallax (moves/zooms a little
    more than the background), bobs, sways, breathes and pops in. Landscape
    keeps the character where the picture drew it; portrait (Shorts) blurs
    the background to cover the tall frame and stands the character in the
    upper half, above the captions. With no cut-out, landscape is camera-only
    and portrait shows the whole picture as the card."""

    def __init__(self, art: dict, w: int, h: int, move: str, phase: float = 0.0):
        import cv2
        bgr = cv2.imread(str(art["bg"]), cv2.IMREAD_COLOR)
        if bgr is None:
            raise AnimateError(f"unreadable illustration {art['bg']}")
        bg = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        self.sh, self.sw = bg.shape[:2]
        self.w, self.h, self.move, self.phase = w, h, move, phase
        self.portrait = h > w
        self.base = max(w / self.sw, h / self.sh)        # cover fit at z = 1
        # upscale once (Lanczos) to the largest zoom, so every per-frame warp
        # only shrinks - sharp and cheap
        self.s_big = self.base * (1 + CAMERA_ZOOM)
        big = cv2.resize(bg, (math.ceil(self.sw * self.s_big), math.ceil(self.sh * self.s_big)),
                         interpolation=cv2.INTER_LANCZOS4)
        if self.portrait:     # ~4x upscale: blur it into a soft depth-of-field backdrop
            big = cv2.GaussianBlur(big, (0, 0), sigmaX=0.008 * w)
        self.big = big

        self.fg, rgba, box = None, None, art.get("box")
        if art.get("fg"):
            raw = cv2.imread(str(art["fg"]), cv2.IMREAD_UNCHANGED)
            if raw is not None and raw.ndim == 3 and raw.shape[2] == 4:
                rgba = cv2.cvtColor(raw, cv2.COLOR_BGRA2RGBA)
        if rgba is None and self.portrait:
            rgba = np.dstack([bg, np.full(bg.shape[:2], 255, np.uint8)])
            box = [0, 0, self.sw, self.sh]
        if rgba is not None and box:
            bx, by, bw, bh = box
            if self.portrait:
                k = min(0.88 * w / bw, 0.40 * h / bh)
            else:
                k = self.base * (1 + CAMERA_ZOOM / 2)
            interp = cv2.INTER_LANCZOS4 if k > 1 else cv2.INTER_AREA
            self.fg = cv2.resize(premultiply(rgba), (max(1, round(bw * k)), max(1, round(bh * k))),
                                 interpolation=interp)
            self.k_fg = k
            self.box_c = (bx + bw / 2, by + bh / 2)
        # portrait backdrop window (~1/3 of the picture's width): aim it at the
        # side the character is NOT on, so its painted-out smudge stays off screen
        self.port_cx = self.sw / 2
        if self.portrait and art.get("fg") and box:
            ww = w / self.base
            left, right = ww / 2, self.sw - ww / 2
            overlap = [max(0.0, min(c + ww / 2, box[0] + box[2]) - max(c - ww / 2, box[0]))
                       for c in (left, right)]
            self.port_cx = left if overlap[0] <= overlap[1] else right

    def frame(self, t: float, start: float, end: float) -> np.ndarray:
        import cv2
        p = clamp01((t - start) / max(end - start, 1e-3))
        z, fx, fy = camera(self.move, p)
        scale = self.base * z                           # output px per source px
        ww, wh = self.w / scale, self.h / scale         # visible window (source px)
        if self.portrait:          # small drift around the chosen side, kept in frame
            cx = min(max(self.port_cx + fx * 0.15 * ww, ww / 2), self.sw - ww / 2)
        else:
            cx = self.sw / 2 + fx * (self.sw - ww) / 2
        cy = self.sh / 2 + fy * (self.sh - wh) / 2
        k = scale / self.s_big
        m = np.float32([[k, 0, -(cx - ww / 2) * scale], [0, k, -(cy - wh / 2) * scale]])
        out = cv2.warpAffine(self.big, m, (self.w, self.h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REFLECT)
        if self.fg is None:
            return out

        age, ph = t - start, self.phase
        pop = 0.85 + 0.15 * ease_out_back(age / 0.6)
        breathe = 1 + 0.02 * math.sin(2 * math.pi * 0.7 * t + ph)
        rot = 2.5 * math.sin(2 * math.pi * 0.3 * t + ph)
        bob = math.sin(2 * math.pi * 0.45 * t + ph) * 0.012 * self.h
        if self.portrait:
            x, y = 0.5 * self.w, 0.31 * self.h
            rel = 1 + (z - 1) * 0.5
        else:
            # parallax: the character travels 25% further than the background
            # and zooms 30% faster, so it reads as closer to the camera
            x = (self.box_c[0] - (cx - ww / 2)) * scale
            y = (self.box_c[1] - (cy - wh / 2)) * scale
            x += -(cx - self.sw / 2) * scale * 0.25
            y += -(cy - self.sh / 2) * scale * 0.25
            rel = self.base * (1 + (z - 1) * 1.3) / self.k_fg
        blit(out, transform(self.fg, rel * breathe * pop, rot), x, y + bob)
        return out


def shot_spans(scenes: list[dict], bounds: list[float]) -> dict[int, tuple]:
    """Pure. art shot id -> (start, end, first scene index): a shot spans the
    consecutive scenes that carry it."""
    spans: dict[int, list] = {}
    for i, sc in enumerate(scenes):
        sid = sc["art"]["shot"]
        if sid in spans:
            spans[sid][1] = bounds[i + 1]
        else:
            spans[sid] = [bounds[i], bounds[i + 1], i]
    return {k: tuple(v) for k, v in spans.items()}


# ====================================================================== render

def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf
    try:
        data, sr = sf.read(str(path), dtype="float32")
    except (OSError, RuntimeError) as e:
        raise AnimateError(f"could not read narration {path}", detail=str(e)) from e
    return (data.mean(axis=1) if data.ndim > 1 else data), int(sr)


def render_track(scenes: list[dict], bounds: list[float], w: int, h: int,
                 fps: int, voice_wav: str | Path, out_path: str | Path,
                 cfg: dict, seed: int = 0) -> Path:
    """Draw and encode the silent cartoon track. See the module docstring for
    the scenes/bounds contract. Raises AnimateError / FFmpegError."""
    import cv2
    if len(bounds) != len(scenes) + 1 or not scenes:
        raise AnimateError(f"{len(scenes)} scenes need {len(scenes) + 1} bounds")
    acfg = cfg["create"]["animation"]
    marks = frame_marks(bounds, fps)
    n = marks[-1]
    lay = layout(w, h)
    t0 = bounds[0]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    illustrated = "art" in scenes[0]
    if any(("art" in sc) != illustrated for sc in scenes):
        raise AnimateError("either every scene carries an illustration or none does")
    samples, sr = _read_mono(Path(voice_wav))
    mouth = mouth_levels(samples, sr, t0 + np.arange(n) / fps, fps)
    eyes = blink_states(n, fps, seed)
    if illustrated and w >= h:     # smaller host so the pictures stay the star
        lay["mascot_h"] *= 0.78
        lay["mascot_x"] *= 0.80
    mascot = get_mascot(int(lay["mascot_h"]))

    # icons per scene: resolved, pre-sized to their slot, with a glow
    prepared, last = [], []
    baked: dict[tuple[int, int], np.ndarray] = {}   # (emoji sprite id, size) -> sprite
    for sc in scenes:
        if illustrated:                 # the pictures replace the emoji
            prepared.append(((), []))
            continue
        sprites = [s for s in (icon_sprite(tok, cfg) for tok in sc.get("icons", [])[:3])
                   if s is not None]
        if not sprites:
            sprites = last or [s for s in [icon_sprite("⭐", cfg)] if s is not None]
        last = sprites
        slots = icon_slots(len(sprites), lay["icons"])
        items = []
        for spr, (cx, cy, size) in zip(sprites, slots):
            # icon baked onto its glow once (and reused across scenes), so each
            # frame warps/pastes one sprite
            key = (id(spr), size)
            if key not in baked:
                icon = cv2.resize(spr, (size, size), interpolation=cv2.INTER_AREA)
                baked[key] = over(glow_sprite(int(size * 1.25)), icon)
            items.append((baked[key], cx, cy, size))
        # key = the sprites actually shown (icon_sprite caches one array per
        # emoji), so a line that fell back to the previous icons doesn't pop
        # them out and back in
        prepared.append((tuple(id(s) for s in sprites), items))

    # banners: independent of scene length (a short first line keeps its banner)
    banners = []
    enter_at = list(bounds[:-1])      # when each scene's icons start popping in
    for i, sc in enumerate(scenes):
        b = sc.get("banner")
        if not b:
            continue
        title = b.get("kind") == "title"
        spr = banner_sprite(b["text"], b.get("label"),
                            int(lay["title_w" if title else "banner_w"]),
                            int(lay["title_h" if title else "banner_h"]),
                            _PALETTE[i % len(_PALETTE)])
        dur = float(acfg.get("title_s" if title else "banner_s", 2.6))
        y = lay["title_y" if title else "banner_y"] + spr.shape[0] / 2
        banners.append((bounds[i], dur, spr, y))
        if title:   # the big title card covers the icon area: every scene that
            title_end = bounds[i] + dur - 0.3        # starts under it waits
            for j in range(i, len(scenes)):
                if bounds[j] >= title_end:
                    break
                enter_at[j] = max(enter_at[j], title_end)

    backgrounds: dict[str, Background] = {}

    def bg(theme):
        if theme not in backgrounds:
            backgrounds[theme] = Background(theme, w, h, seed)
        return backgrounds[theme]

    spans = shot_spans(scenes, bounds) if illustrated else {}
    shots: dict[int, ArtShot] = {}

    def shot(sid):
        if sid not in shots:
            for old in [k for k in shots if spans[k][1] < spans[sid][0]]:
                del shots[old]          # finished shots: free their big buffers
            first = spans[sid][2]
            shots[sid] = ArtShot(scenes[first]["art"], w, h,
                                 CAMERA_MOVES[(sid + seed) % len(CAMERA_MOVES)],
                                 phase=sid * 1.3)
        return shots[sid]

    def art_frame(si, t):
        sid = scenes[si]["art"]["shot"]
        a, b, first = spans[sid]
        out = shot(sid).frame(t, a, b)
        if first > 0 and t - a < xfade:             # cross-fade from the previous picture
            pid = scenes[first - 1]["art"]["shot"]
            pa, pb, _ = spans[pid]
            mix = clamp01((t - a) / xfade)
            cv2.addWeighted(shot(pid).frame(t, pa, pb), 1 - mix, out, mix, 0, out)
        return out

    xfade = float(acfg.get("crossfade_s", 0.5))
    log.info("animating %d frames (%dx%d @ %dfps, %d scenes)", n, w, h, fps,
             len(scenes))
    si = 0
    next_log = 0.1
    with RawVideoWriter(out_path, w, h, fps, video_encode_args(cfg)) as wr:
        for k in range(n):
            while k >= marks[si + 1]:
                si += 1
            t = t0 + k / fps
            s0, s1 = bounds[si], bounds[si + 1]
            theme = scenes[si].get("scene", "sky")
            if illustrated:
                frame = art_frame(si, t)
            else:
                frame = bg(theme).frame(t)
            if (not illustrated and si > 0 and scenes[si - 1].get("scene") != theme
                    and t - s0 < xfade):
                a = clamp01((t - s0) / xfade)
                cv2.addWeighted(bg(scenes[si - 1].get("scene")).frame(t), 1 - a,
                                frame, a, 0, frame)

            # icons: pop in (unless same as the previous line), float, pop out
            key, items = prepared[si]
            enter = si == 0 or prepared[si - 1][0] != key
            leave = si == len(scenes) - 1 or prepared[si + 1][0] != key
            for j, (spr, cx, cy, size) in enumerate(items):
                ph = j * 1.7 + si * 0.9
                scale, rot = 1.0, 0.0
                if enter or enter_at[si] > s0:
                    local = (t - enter_at[si]) - j * 0.15
                    if local < 0:
                        continue
                    p = clamp01(local / 0.45)
                    scale = ease_out_back(p)
                    rot = (1 - ease_out_cubic(p)) * -22
                if leave:
                    scale *= 1 - ease_in_cubic(clamp01((0.25 - (s1 - t)) / 0.25))
                if scale <= 0.02:
                    continue
                fy = math.sin(2 * math.pi * 0.55 * t + ph) * size * 0.035
                rot += 4 * math.sin(2 * math.pi * 0.35 * t + ph)
                breathe = 1 + 0.02 * math.sin(2 * math.pi * 0.8 * t + ph)
                blit(frame, transform(spr, scale * breathe, rot), cx, cy + fy)

            # mascot: lip flap + blink + bob; waves while a banner is up
            lvl = int(mouth[k])
            talking = int(mouth[max(0, k - 6):k + 1].max()) > 0
            waving = any(b0 <= t < b0 + d for b0, d, _, _ in banners)
            if waving:
                wing = 2 if int(t * 4) % 2 == 0 else 1
            elif talking:
                wing = 1 if int(t * 1.5) % 3 == 1 else 0
            else:
                wing = 0
            spr = mascot.poses[(lvl, int(eyes[k]), wing)]
            mh = mascot.height
            if talking:
                dy = -abs(math.sin(2 * math.pi * 1.1 * t)) * mh * 0.03
                sx = 1 + 0.015 * math.sin(2 * math.pi * 2.2 * t)
                spr = cv2.resize(spr, (int(spr.shape[1] * sx), int(spr.shape[0] * (2 - sx))),
                                 interpolation=cv2.INTER_LINEAR)
            else:
                dy = math.sin(2 * math.pi * 0.4 * t) * mh * 0.006
            blit(frame, spr, lay["mascot_x"], lay["mascot_bottom"] - spr.shape[0] / 2 + dy)

            # banners slide down with a bounce, slide back up at the end
            for b0, dur, bspr, by in banners:
                age = t - b0
                if not 0 <= age < dur:
                    continue
                hh = bspr.shape[0]
                off = (1 - ease_out_back(age / 0.5)) * (by + hh)
                off += ease_in_cubic(clamp01((age - (dur - 0.35)) / 0.35)) * (by + hh)
                blit(frame, bspr, w / 2, by - off)

            wr.write(frame)
            if (k + 1) / n >= next_log:
                log.info("animate %s: %d%%", out_path.parent.name, int(next_log * 100))
                next_log += 0.1
    return out_path
