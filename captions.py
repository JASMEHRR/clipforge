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
from ffutil import filter_path, probe, run_ffmpeg
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

def write_ass(words: list[dict], ass_path: Path, cfg: dict, preset_name: str,
              play_w: int = 1080, play_h: int = 1920) -> None:
    ccfg = cfg["captions"]
    preset = ccfg["presets"][preset_name]
    family, bold = _font(preset["font"])
    margin_v = int(ccfg["bottom_margin_px"])
    scale = int(preset.get("highlight_scale", 100))
    style_kind = preset.get("style", "karaoke")

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

    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        for start, end, text in events:
            f.write(f"Dialogue: 0,{_ts(start)},{_ts(end)},Base,,0,0,0,,{text}\n")


def write_srt(words: list[dict], srt_path: Path, cfg: dict) -> None:
    lines = build_caption_lines(words, int(cfg["captions"]["max_words_per_line"]))
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines, 1):
            text = " ".join(w["word"] for w in line["words"])
            f.write(f"{i}\n{_srt_ts(line['start'])} --> "
                    f"{_srt_ts(line['end'])}\n{text}\n\n")


# --------------------------------------------------------------- burn entry

def caption_clip(video_path: str | Path, words: list[dict],
                 out_path: str | Path, cfg: dict | None = None,
                 preset_name: str | None = None) -> Path:
    """Burn animated captions onto a clip; also writes .ass and .srt next to
    the output. `words` must already be clip-relative. Empty words → video is
    passed through re-encoded (mechanical runs) and an empty .srt is written."""
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
    write_srt(words, srt_path, cfg)

    r = cfg["render"]
    if not words:
        log.warning("no words for %s — burning skipped (mechanical run)",
                    video_path.name)
        run_ffmpeg(["-i", video_path, "-c:v", "libx264",
                    "-preset", r["preset_final"], "-crf", str(r["crf"]),
                    "-c:a", "copy", out_path])
        return out_path

    info = probe(video_path)
    write_ass(words, ass_path, cfg, preset_name,
              play_w=info["width"], play_h=info["height"])
    fontsdir = ROOT / cfg["captions"]["font_dir"]
    vf = (f"subtitles=filename='{filter_path(ass_path)}'"
          f":fontsdir='{filter_path(fontsdir)}'")
    run_ffmpeg(["-i", video_path, "-vf", vf,
                "-c:v", "libx264", "-preset", r["preset_final"],
                "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
                "-c:a", "copy", out_path])
    log.info("captions burned (%s, %d words) -> %s",
             preset_name, len(words), out_path.name)
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
