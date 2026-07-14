"""Animated captions: ASS karaoke burned in with FFmpeg.

- current word highlighted (color pop + slight scale), 3-4 words per line max
- safe margins (bottom_margin_px) keep text out of platform UI zones
- bundled fonts only: the subtitles filter gets fontsdir=assets/fonts
- presets: karaoke (word pop), fade (single line, subtle fade),
  box (active word in a filled box via a BorderStyle=3 sub-style)
- an .srt is exported alongside every burned clip"""
from __future__ import annotations

import argparse
from pathlib import Path

from config import ROOT, load_config
from errors import CaptionError
from ffutil import filter_path, probe, run_ffmpeg, video_encode_args
from logutil import get_logger

log = get_logger("captions")


# --------------------------------------------------------- pure line builder

def build_caption_lines(words: list[dict], max_words: int) -> list[dict]:
    """Group clip-relative words into caption lines (≤ max_words each).
    Pure, unit-tested. Line end extends to the next line's start (no flicker),
    capped at +0.6s after the last word."""
    lines = []
    for i in range(0, len(words), max_words):
        chunk = words[i:i + max_words]
        lines.append({"words": chunk, "start": chunk[0]["start"],
                      "end": chunk[-1]["end"]})
    for k, line in enumerate(lines):
        nxt = lines[k + 1]["start"] if k + 1 < len(lines) else line["end"] + 0.6
        line["end"] = max(line["end"], min(nxt, line["end"] + 0.6))
    return lines


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def _esc(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def _font(name: str) -> tuple[str, int]:
    """Map config font names to (ASS family, bold flag). Non-RIBBI weights
    (ExtraBold/Black) are their own families in the bundled TTFs."""
    if name.endswith(" Regular"):
        return name[:-8].strip(), 0
    if name.endswith(" Bold"):
        return name[:-5].strip(), -1
    return name, 0


# ------------------------------------------------------------- ASS writing

def _clamp_anchor(value: float) -> float:
    """CAPTION POSITION LAW: block center inside [0.52, 0.66]."""
    return min(0.66, max(0.52, float(value)))


_WM_XY = {
    "top-left":     ("{m}", "{m}"),
    "top-right":    ("w-tw-{m}", "{m}"),
    "bottom-left":  ("{m}", "h-th-{m}"),
    "bottom-right": ("w-tw-{m}", "h-th-{m}"),
    "center":       ("(w-tw)/2", "(h-th)/2"),
}


def watermark_filter(wm: dict) -> str:
    """Build a drawtext filter for the brand/handle overlay. Pure (returns the
    filter string); off-by-default so absent config never adds a filter."""
    text = str(wm.get("text", "")).replace("\\", "").replace(":", r"\:") \
        .replace("'", "").replace("%", "")
    size = int(wm.get("font_size", 36))
    op = max(0.0, min(1.0, float(wm.get("opacity", 0.6))))
    margin = int(wm.get("margin_px", 40))
    x, y = _WM_XY.get(wm.get("position", "bottom-right"), _WM_XY["bottom-right"])
    font = ROOT / wm.get("font_file", "assets/fonts/Montserrat-Bold.ttf")
    return (f"drawtext=fontfile='{filter_path(font)}':text='{text}'"
            f":fontsize={size}:fontcolor=white@{op:.2f}"
            f":x={x.format(m=margin)}:y={y.format(m=margin)}"
            f":box=1:boxcolor=black@{op * 0.4:.2f}:boxborderw=8")


# overlay=x:y expressions for an image logo (W/H = frame, w/h = scaled logo)
_LOGO_XY = {
    "top-left":     ("{m}", "{m}"),
    "top-right":    ("W-w-{m}", "{m}"),
    "bottom-left":  ("{m}", "H-h-{m}"),
    "bottom-right": ("W-w-{m}", "H-h-{m}"),
    "center":       ("(W-w)/2", "(H-h)/2"),
}


def _wm_mode(wm: dict) -> str:
    """Watermark mode with backward compatibility: an explicit off|text|image
    wins; legacy configs (no `mode`) map `enabled` → text, else off."""
    m = str(wm.get("mode", "") or "").lower()
    if m in ("off", "text", "image"):
        return m
    return "text" if wm.get("enabled") else "off"


def zoom_crop_vf(events: list[dict], w: int, h: int, fps: float) -> str:
    """Per-frame punch-in zoom via zoompan (crop w/h can't vary over time):
    zoom rises by each event's amount with a 0.12 s ease-in and 0.15 s
    ease-out, centered. `it` is zoompan's input timestamp, so one expression
    handles every event without conflicts."""
    terms = []
    for ev in events:
        t0, t1 = float(ev["t"]), float(ev["t"]) + float(ev["dur"])
        terms.append(f"{float(ev['amount']):.4f}"
                     f"*min(1,max(0,(it-{t0:.3f})/0.12))"
                     f"*min(1,max(0,({t1:.3f}-it)/0.15))")
    z = "1+" + "+".join(terms)
    return (f"zoompan=z='{z}':d=1"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={fps:g}")


def whip_blur_vf(times: list[float], dur: float = 0.12) -> str:
    """'Whip' transition: a hard blur burst over each timeline join. Duration-
    neutral (no frames added/removed), so word timing stays exact."""
    windows = "+".join(f"between(t\\,{max(0.0, t - dur / 2):.3f}\\,"
                       f"{t + dur / 2:.3f})" for t in times)
    return f"boxblur=luma_radius=12:luma_power=1:enable='{windows}'"


def _popin_chain(start_label: str, popins: list[dict], input_offset: int,
                 png_w: int) -> tuple[list[str], str]:
    """Overlay chain for keyword pop-in PNGs (each an extra ffmpeg input,
    scaled to png_w and shown centered in the upper third for its event
    window). Returns the graph parts and the final label."""
    parts, prev = [], start_label
    for k, ev in enumerate(popins):
        idx = input_offset + k
        t0, t1 = float(ev["t"]), float(ev["t"]) + float(ev["dur"])
        parts.append(f"[{idx}:v]format=rgba,scale={png_w}:-1[pi{k}]")
        parts.append(
            f"[{prev}][pi{k}]overlay=(W-w)/2:H*0.28-h/2"
            f":enable='between(t,{t0:.3f},{t1:.3f})'[po{k}]")
        prev = f"po{k}"
    return parts, prev


def _logo_graph(vf_parts: list[str], wm: dict) -> str:
    """One-pass video filtergraph for an alpha logo overlay. Applies the caption/
    fade chain to the source, scales the logo (ffmpeg input 1) to a fraction of
    the FRAME width preserving its aspect and PNG transparency, and overlays it.
    Ends at label [vout]. scale2ref keeps it a single encode (no second pass)."""
    scale = max(0.01, min(1.0, float(wm.get("scale", 0.12))))
    op = max(0.0, min(1.0, float(wm.get("opacity", 0.85))))
    margin = int(wm.get("margin_px", 40))
    x, y = _LOGO_XY.get(wm.get("position", "top-right"), _LOGO_XY["top-right"])
    base = ",".join(vf_parts) if vf_parts else "null"
    return (f"[1:v]format=rgba,colorchannelmixer=aa={op:.2f}[wm0];"
            f"[0:v]{base}[base0];"
            f"[wm0][base0]scale2ref=w=main_w*{scale:g}:h=ow/a[wm][base];"
            f"[base][wm]overlay={x.format(m=margin)}:{y.format(m=margin)}[vout]")


def write_ass(words: list[dict], ass_path: Path, cfg: dict, preset_name: str,
              play_w: int = 1080, play_h: int = 1920,
              anchor: float | None = None, cta: dict | None = None,
              clip_duration: float | None = None) -> None:
    ccfg = cfg["captions"]
    preset = ccfg["presets"][preset_name]
    family, bold = _font(preset["font"])
    margin_v = int(ccfg["bottom_margin_px"])
    scale = int(preset.get("highlight_scale", 100))
    style_kind = preset.get("style", "karaoke")

    # Position law: when an anchor is given, every line's CENTER is pinned via
    # \an5\pos at anchor*height (clamped to the band). anchor=None keeps the
    # legacy bottom-margin behaviour byte-for-byte.
    cx = play_w // 2
    pos_tag = ""
    if anchor is not None:
        pos_tag = rf"{{\an5\pos({cx},{int(_clamp_anchor(anchor) * play_h)})}}"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{family},{preset['font_size']},{preset['primary_color']},{preset['highlight_color']},{preset['outline_color']},&H80000000,{bold},0,0,0,100,100,0,0,1,{preset['outline']},{preset['shadow']},2,90,90,{margin_v},1
"""
    if style_kind == "box":
        box = preset.get("box_color", "&H00E16B16")
        header += (f"Style: BoxActive,{family},{preset['font_size']},"
                   f"{preset['primary_color']},{preset['primary_color']},"
                   f"{box},{box},{bold},0,0,0,100,100,0,0,3,12,0,2,90,90,"
                   f"{margin_v},1\n")
    header += "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"

    lines = build_caption_lines(words, int(ccfg["max_words_per_line"]))
    events = []
    for line in lines:
        tokens = [_esc(w["word"].upper() if preset.get("uppercase")
                       else w["word"]) for w in line["words"]]
        if style_kind == "fade":
            events.append((line["start"], line["end"],
                           r"{\fad(150,150)}" + " ".join(tokens)))
            continue
        for k, w in enumerate(line["words"]):
            start = line["start"] if k == 0 else w["start"]
            end = (line["words"][k + 1]["start"] if k + 1 < len(line["words"])
                   else line["end"])
            if end - start < 0.01:
                continue
            parts = []
            for j, tok in enumerate(tokens):
                if j != k:
                    parts.append(tok)
                elif style_kind == "box":
                    parts.append(r"{\rBoxActive}" + tok + r"{\r}")
                else:
                    parts.append(r"{\c" + preset["highlight_color"] + "&"
                                 + rf"\fscx{scale}\fscy{scale}" + "}"
                                 + tok + r"{\r}")
            events.append((start, end, " ".join(parts)))

    # CTA overlay: styled per active preset, positioned per the law, shown in
    # the final cta.duration_s. Nudged just above the caption anchor so it does
    # not sit on top of a trailing caption line.
    if cta and cta.get("enabled") and clip_duration:
        dur_cta = float(cta.get("duration_s", 1.5))
        c_start = max(0.0, clip_duration - dur_cta)
        text = _esc(cta.get("text", "Follow for more"))
        if preset.get("uppercase"):
            text = text.upper()
        if anchor is not None:
            cta_y = int(_clamp_anchor(anchor - 0.08) * play_h)
            cta_tag = rf"{{\an5\pos({cx},{cta_y})\b1}}"
        else:
            cta_tag = r"{\an2\b1}"
        events.append((c_start, float(clip_duration), cta_tag + text))

    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        for start, end, text in events:
            # CTA already carries its own \pos; caption lines get the block pos.
            prefix = "" if text.startswith(r"{\an") else pos_tag
            f.write(f"Dialogue: 0,{_ts(start)},{_ts(end)},Base,,0,0,0,,{prefix}{text}\n")


def write_srt(words: list[dict], srt_path: Path, cfg: dict) -> None:
    lines = build_caption_lines(words, int(cfg["captions"]["max_words_per_line"]))
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines, 1):
            text = " ".join(w["word"] for w in line["words"])
            f.write(f"{i}\n{_srt_ts(line['start'])} --> "
                    f"{_srt_ts(line['end'])}\n{text}\n\n")


# --------------------------------------------------------------- burn entry

def cta_from_cfg(cfg: dict) -> dict:
    """caption_clip kwargs carrying the CTA for the NO-refine path: the config
    `style.cta` dict when enabled with non-blank text, else `{}` (adds nothing,
    preserving today's behaviour). Used by both pipeline._render_one and
    rerender_clip so CTA text is not silently dropped when Style Refinement is
    off (the refine path supplies its own CTA via the edit plan)."""
    cta = (cfg.get("style") or {}).get("cta") or {}
    if cta.get("enabled") and str(cta.get("text", "")).strip():
        return {"cta": cta}
    return {}


def caption_clip(video_path: str | Path, words: list[dict],
                 out_path: str | Path, cfg: dict | None = None,
                 preset_name: str | None = None,
                 anchor: float | None = None, cta: dict | None = None,
                 captions_enabled: bool = True, fades: dict | None = None,
                 zoom_punch: bool = False,
                 zoom_events: list[dict] | None = None,
                 popin_events: list[dict] | None = None,
                 whip_times: list[float] | None = None) -> Path:
    """Burn animated captions onto a clip; also writes .ass and .srt next to
    the output. `words` must already be clip-relative. Empty words → video is
    passed through re-encoded (mechanical runs) and an empty .srt is written.

    Style-refiner inputs (all default to today's behaviour, so style-off output
    is byte-identical):
      anchor — CAPTION POSITION LAW block center (clamped to [0.52,0.66]).
      cta — {enabled,text,duration_s} overlay in the final seconds.
      captions_enabled=False — KEEP mode: burn no captions, no .srt (a source
        clip already carries its own subtitles).
      fades — {audio_in_ms,audio_out_ms,video_out_ms} envelope (no hard edges).
      zoom_punch — subtle 1.0->scale punch-in over the first zoom_seconds
        (weak-hook enhancement).
      zoom_events — punch-in zooms [{t,dur,amount}] in clip time (preset
        punch_in / zoom transitions).
      popin_events — keyword graphics [{t,dur,asset}]; missing PNGs are
        skipped with a warning, never fail the burn.
      whip_times — 'whip' transition blur bursts at these clip times."""
    cfg = cfg or load_config()
    preset_name = preset_name or cfg["captions"]["preset"]
    if preset_name not in cfg["captions"]["presets"]:
        raise CaptionError(f"unknown caption preset '{preset_name}'")
    video_path, out_path = Path(video_path), Path(out_path)
    if not video_path.exists():
        raise CaptionError(f"video not found: {video_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ass_path = out_path.with_suffix(".ass")
    srt_path = out_path.with_suffix(".srt")
    info = probe(video_path)
    dur = float(info["duration"])
    burn = captions_enabled and bool(words)
    if captions_enabled:
        write_srt(words, srt_path, cfg)

    vf_parts: list[str] = []
    scfg = cfg.get("style", {})
    if zoom_punch:
        z = float(scfg.get("zoom_punch_scale", 1.06))
        zs = float(scfg.get("zoom_punch_seconds", 1.5))
        fps = float(info.get("fps", 30.0)) or 30.0
        inc = (z - 1.0) / max(1.0, zs * fps)
        vf_parts.append(
            f"zoompan=z='min(zoom+{inc:.6f},{z})':d=1"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={info['width']}x{info['height']}:fps={fps:g}")
    if zoom_events:
        vf_parts.append(zoom_crop_vf(zoom_events, info["width"],
                                     info["height"],
                                     float(info.get("fps", 30.0)) or 30.0))
    if burn:
        write_ass(words, ass_path, cfg, preset_name,
                  play_w=info["width"], play_h=info["height"],
                  anchor=anchor, cta=cta, clip_duration=dur)
        import fontreg
        fontsdir = fontreg.fonts_dir(cfg)       # bundled dir, or combined w/ user fonts
        vf_parts.append(f"subtitles=filename='{filter_path(ass_path)}'"
                        f":fontsdir='{filter_path(fontsdir)}'")
    wm = cfg["captions"].get("watermark", {})
    wm_mode = _wm_mode(wm)
    if wm_mode == "text" and str(wm.get("text", "")).strip():
        vf_parts.append(watermark_filter(wm))
    logo = None
    if wm_mode == "image":
        ip = str(wm.get("image_path", "") or "").strip()
        logo = (ROOT / ip) if ip else None
        if not (logo and logo.exists()):
            log.warning("watermark image mode but image_path missing (%r); "
                        "rendering without logo", ip)
            logo = None
    if whip_times:
        vf_parts.append(whip_blur_vf(whip_times))
    if fades and float(fades.get("video_out_ms", 0)) > 0:
        vo = float(fades["video_out_ms"]) / 1000.0
        vf_parts.append(f"fade=t=out:st={max(0.0, dur - vo):.3f}:d={vo:.3f}")

    # pop-in graphics: extra PNG inputs (after the logo when present)
    popins = []
    for ev in (popin_events or []):
        p = Path(ev["asset"])
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            popins.append({**ev, "asset": p})
        else:
            log.warning("pop-in asset missing, skipped: %s", ev["asset"])

    # audio fade chain (shared: -af on the plain path, filtergraph on the logo path)
    af = []
    if fades:
        ain = float(fades.get("audio_in_ms", 0)) / 1000.0
        aout = float(fades.get("audio_out_ms", 0)) / 1000.0
        if ain > 0:
            af.append(f"afade=t=in:st=0:d={ain:.3f}")
        if aout > 0:
            af.append(f"afade=t=out:st={max(0.0, dur - aout):.3f}:d={aout:.3f}")

    args = ["-i", video_path]
    if logo or popins:
        # Overlays (alpha logo and/or pop-in PNGs) in the same encode.
        # -filter_complex cannot coexist with -af for the same file, so route
        # the audio fade through the graph.
        if logo:
            args += ["-i", logo]
            graph = _logo_graph(vf_parts, wm)   # ends at [vout]
        else:
            base = ",".join(vf_parts) if vf_parts else "null"
            graph = f"[0:v]{base}[vout]"
        if popins:
            for ev in popins:
                args += ["-i", ev["asset"]]
            png_w = max(64, (int(info["width"] * 0.18) // 2) * 2)
            chain, out_label = _popin_chain("vout", popins,
                                            input_offset=2 if logo else 1,
                                            png_w=png_w)
            graph = ";".join([graph] + chain)
        else:
            out_label = "vout"
        maps = ["-map", f"[{out_label}]"]
        if af:
            graph += f";[0:a]{','.join(af)}[aout]"
            maps += ["-map", "[aout]"]
        else:
            maps += ["-map", "0:a?"]
        args += ["-filter_complex", graph] + maps
        args += video_encode_args(cfg, final=True)
        args += (["-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"]]
                 if af else ["-c:a", "copy"])
    else:
        if vf_parts:
            args += ["-vf", ",".join(vf_parts)]
        args += video_encode_args(cfg, final=True)
        if af:
            args += ["-af", ",".join(af), "-c:a", "aac",
                     "-b:a", cfg["render"]["audio_bitrate"]]
        else:
            args += ["-c:a", "copy"]
    args.append(out_path)
    run_ffmpeg(args)
    log.info("captions %s (%s, %d words)%s -> %s",
             "burned" if burn else "skipped(keep)", preset_name, len(words),
             " +cta" if (cta and cta.get('enabled') and burn) else "",
             out_path.name)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: caption a clip")
    ap.add_argument("video")
    ap.add_argument("--out", default="output/_smoke_captions.mp4")
    ap.add_argument("--preset", default=None)
    a = ap.parse_args()
    demo_words = []
    t = 0.5
    for tok in ("This is a smoke test of animated karaoke captions "
                "rendered with the bundled Montserrat font").split():
        demo_words.append({"word": tok, "start": round(t, 2),
                           "end": round(t + 0.32, 2)})
        t += 0.38
    print(caption_clip(a.video, demo_words, a.out, preset_name=a.preset))
