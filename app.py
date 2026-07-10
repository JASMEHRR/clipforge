"""Gradio UI (gradio 6.x): Create / Batch / Edit / Upload / History tabs.

All long work runs in background threads; progress streams through queues so
the UI thread never blocks. The YouTube OAuth flow is only ever started by an
explicit user click when client secrets are configured (never during builds)."""
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import traceback
from pathlib import Path

import gradio as gr

from config import ROOT, load_config
from logutil import get_logger

log = get_logger("app")

BRANDING_DIR = ROOT / "assets" / "user_branding"


def _persist_branding(upload_path: str | None) -> str:
    """Copy an uploaded logo into assets/user_branding/ so it survives Gradio's
    temp-cache cleanup and process restarts, returning its repo-relative path
    (''=no upload). Re-uploading the same filename replaces it."""
    if not upload_path:
        return ""
    src = Path(upload_path)
    if not src.exists():
        log.warning("logo upload path does not exist: %s", upload_path)
        return ""
    try:
        BRANDING_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name) or "logo.png"
        dest = BRANDING_DIR / safe
        shutil.copyfile(src, dest)
        return str(dest.relative_to(ROOT)).replace("\\", "/")
    except OSError as e:
        log.warning("could not persist logo %s: %s", src, e)
        return ""


# --------------------------------------------------------------- create tab

def _resolve_music_choice(choice: str) -> str:
    """Map a UI music choice onto what music.resolve accepts. 'random' picks a
    concrete track id here (at enqueue/run start) so each batch job can get a
    different track."""
    if choice == "random":
        import random as _random

        import music
        try:
            ids = [t["id"] for t in music.list_tracks()]
        except Exception as e:  # noqa: BLE001 — manifest optional
            log.warning("music manifest unavailable: %s", e)
            ids = []
        return _random.choice(ids) if ids else ""
    return choice or ""


def _music_card_html(t: dict) -> str:
    """Track card: title, license, moods, attribution note when required."""
    moods = ", ".join(t.get("moods", []))
    bits = [f"license: {t.get('license', '?')}"]
    if moods:
        bits.append(f"mood: {moods}")
    if t.get("attribution_required"):
        bits.append("credit added to the video description automatically")
    return (f"<div><b>{_esc_html(t.get('title', t.get('id', '?')))}</b><br>"
            f"<span style='opacity:.7'>{_esc_html(' · '.join(bits))}</span></div>")


def _music_preview(track_id: str):
    """Return the local path for the shared preview player, downloading the
    track on first use (manifest tracks are fetched lazily)."""
    import music
    gr.Info("Loading track — first play may take a few seconds…")
    try:
        return str(music.ensure_track(music.get_track(track_id)))
    except Exception as e:  # noqa: BLE001 — preview failure must not crash UI
        log.warning("music preview failed for %s: %s", track_id, e)
        raise gr.Error("Couldn't load that track right now. Check your "
                       "internet connection and try again.")


def _profile_choices():
    """Stems of profiles/*.json for the style-profile dropdown."""
    from config import ROOT
    pdir = ROOT / "profiles"
    return sorted(p.stem for p in pdir.glob("*.json")) if pdir.exists() else ["default"]


# ------------------------------------------------- popup picker card HTML
# Pure builders (module-level so tests can assert their content). Each card
# is plain-language: a small visual mock + one-line description, no jargon.

_ASPECT_CARDS = {
    "9:16": ("Vertical", "Tall portrait — Shorts, Reels, TikTok"),
    "1:1": ("Square", "Square — feed posts"),
    "16:9": ("Widescreen", "Landscape — regular YouTube"),
}

_SUBS_CARDS = {
    "auto": ("Decide for me", "ClipForge checks each clip and picks the best "
             "handling automatically"),
    "replace": ("Replace them", "Hide the video's own subtitles and burn fresh "
                "ClipForge captions"),
    "keep": ("Keep them", "Keep the video's own subtitles and don't add new "
             "captions on top"),
    "ignore": ("Ignore them", "Leave the video as-is and burn ClipForge "
               "captions anyway"),
}

_WMPOS_CARDS = {
    "top-left": "Top left", "top-right": "Top right",
    "bottom-left": "Bottom left", "bottom-right": "Bottom right",
    "center": "Center",
}


def _aspect_card_html(value: str) -> str:
    title, desc = _ASPECT_CARDS[value]
    w, h = {"9:16": (40, 71), "1:1": (56, 56), "16:9": (96, 54)}[value]
    return (f"<div style='display:flex;gap:14px;align-items:center'>"
            f"<div style='width:{w}px;height:{h}px;border:2px solid currentColor;"
            f"border-radius:6px;opacity:.7'></div>"
            f"<div><b>{title} ({value})</b><br><span style='opacity:.7'>"
            f"{desc}</span></div></div>")


def _subs_card_html(value: str) -> str:
    title, desc = _SUBS_CARDS[value]
    return (f"<div><b>{title}</b><br>"
            f"<span style='opacity:.7'>{desc}</span></div>")


def _wmpos_card_html(value: str) -> str:
    title = _WMPOS_CARDS[value]
    v, hz = value.split("-") if "-" in value else ("center", "center")
    top = {"top": "6px", "bottom": "auto", "center": "28px"}[v]
    bottom = "6px" if v == "bottom" else "auto"
    left = {"left": "6px", "right": "auto", "center": "26px"}[hz]
    right = "6px" if hz == "right" else "auto"
    return (f"<div style='display:flex;gap:14px;align-items:center'>"
            f"<div style='position:relative;width:40px;height:66px;"
            f"border:2px solid currentColor;border-radius:6px;opacity:.7'>"
            f"<div style='position:absolute;top:{top};bottom:{bottom};"
            f"left:{left};right:{right};width:9px;height:9px;border-radius:50%;"
            f"background:currentColor'></div></div>"
            f"<div><b>{title}</b><br><span style='opacity:.7'>Your watermark "
            f"or logo sits here</span></div></div>")


def _profile_card_html(name: str, data: dict) -> str:
    """Text-summary card for a style profile: name + the key style fields the
    profile JSON actually carries (skips absent ones)."""
    bits = []
    caps = data.get("captions") or {}
    if caps.get("preset"):
        bits.append(f"caption style: {caps['preset']}")
    if caps.get("font"):
        bits.append(f"font: {caps['font']}")
    if data.get("pacing", {}).get("target_wpm"):
        bits.append(f"pacing: ~{data['pacing']['target_wpm']} wpm")
    hook = data.get("hook", {}).get("style") or data.get("hook_style")
    if hook:
        bits.append(f"hooks: {hook}")
    desc = " · ".join(_esc_html(b) for b in bits) or \
        "Custom style learned from your reference videos"
    return (f"<div><b>{_esc_html(name)}</b><br>"
            f"<span style='opacity:.7'>{desc}</span></div>")


def _static_picker_modal(title, intro, cards, state, label, label_prefix):
    """Build a cf-modal overlay of static cards. Must be called inside Blocks.

    cards: (value, display, html) triples. Picking one writes value→state,
    updates the label Markdown, and hides the modal."""
    with gr.Column(visible=False, elem_classes=["cf-modal"]) as modal:
        gr.Markdown(f"### {title}\n{intro}")
        close = gr.Button("Close")
        for value, display, html in cards:
            with gr.Column(elem_classes=["cf-font-row"]):
                gr.HTML(html)
                b = gr.Button(f"Use: {display}", size="sm")
                b.click(
                    (lambda v=value, d=display:
                     (v, f"**{label_prefix}:** {d}", gr.update(visible=False))),
                    None, [state, label, modal])
    close.click(lambda: gr.update(visible=False), None, [modal])
    return modal


def _virality_badge(vir: dict | None) -> str:
    """Green >=70 / yellow 40-69 / red <40 virality badge for the score table."""
    if not vir:
        return "-"
    score = vir.get("score", 0)
    dot = "🟢" if score >= 70 else "🟡" if score >= 40 else "🔴"
    return f"{dot} {int(score)} ({vir.get('verdict', '?')})"


# ---------------------------------------------------------- card gallery (F2)

_BAND_COLOR = {"Strong": "#1a7f37", "Promising": "#9a6700", "Weak": "#b42318"}


def _esc_html(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt_ts(seconds: float) -> str:
    """mm:ss, or hh:mm:ss once the source passes an hour."""
    s = int(round(max(0.0, float(seconds))))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _source_html(c: dict) -> str:
    """'Source: mm:ss–mm:ss · name' from provenance fields; '' for old jobs
    (rendered before provenance was tracked) that lack the bounds."""
    a = c.get("original_source_start_s")
    b = c.get("original_source_end_s")
    if a is None or b is None:
        return ""
    name = str(c.get("source_name") or "").strip()
    if len(name) > 40:
        name = name[:37] + "…"
    tail = f" · {_esc_html(name)}" if name else ""
    return (f"<div class='cf-source'>Source: {_fmt_ts(a)}–{_fmt_ts(b)}{tail}</div>")


def _signals_html(vir: dict) -> str:
    """Expandable per-signal breakdown (name, 0-10 bar, reason)."""
    rows = []
    for s in vir.get("signals", []):
        pct = max(0.0, min(10.0, float(s.get("score", 0)))) * 10.0
        rows.append(
            f"<div class='cf-sig'><span class='cf-sig-name'>{_esc_html(s['name'])}</span>"
            f"<span class='cf-bar'><span class='cf-bar-fill' style='width:{pct:.0f}%'></span></span>"
            f"<span class='cf-sig-score'>{s.get('score', 0):.1f}</span>"
            f"<span class='cf-sig-reason'>{_esc_html(s.get('reason', ''))}</span></div>")
    return "".join(rows)


_EVENT_EMOJI = {"laughter": "😂", "strong_reaction": "🤯",
                "physical_event": "⚡", "reveal": "🎁",
                "expression_shift": "😮", "energy_spike": "🔥",
                "profound_statement": "💬", "conflict": "⚔️",
                "celebration": "🎉", "other": "✨"}


def _events_html(c: dict) -> str:
    """viral_v2 audit line: '😂 laughter 3x · ⚡ physical_event at 0:14'."""
    events = c.get("events") or []
    if not events:
        return ""
    clip_start = c.get("start", 0.0)
    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)
    parts = []
    for etype, evs in by_type.items():
        emoji = _EVENT_EMOJI.get(etype, "✨")
        if len(evs) > 1:
            parts.append(f"{emoji} {_esc_html(etype)} {len(evs)}x")
        else:
            t = max(0.0, evs[0]["t_start_s"] - clip_start)
            parts.append(f"{emoji} {_esc_html(etype)} at {_fmt_ts(t)}")
    return f"<div class='cf-source'>{' · '.join(parts)}</div>"


def _clip_card(rank: int, c: dict) -> str:
    vir = c.get("virality") or {}
    band = vir.get("band") or ("Strong" if vir.get("score", 0) >= 70
                               else "Promising" if vir.get("score", 0) >= 45
                               else "Weak")
    color = _BAND_COLOR.get(band, "#57606a")
    title = _esc_html((c.get("metadata") or {}).get("title", f"Clip {c.get('index', rank)}"))
    dur = c.get("duration", 0)
    render_s = c.get("render_s")
    quality = c.get("weighted_score")
    flags = ((c.get("style") or {}) or {}).get("flags") or []
    flag_html = "".join(f"<span class='cf-flag'>{_esc_html(f)}</span>" for f in flags)
    meta = [f"⏱ {dur:.0f}s"]
    if quality is not None:
        meta.append(f"★ {quality:.2f} quality")
    if render_s is not None:
        meta.append(f"⚙ rendered in {render_s:.0f}s")
    meta.append(f"🎬 {_esc_html(c.get('preset', ''))}")
    breakdown = _signals_html(vir)
    details = (f"<details class='cf-details'><summary>engagement signals</summary>"
               f"{breakdown}</details>") if breakdown else ""
    return (
        f"<div class='cf-card'>"
        f"<div class='cf-card-head'>"
        f"<span class='cf-rank'>#{rank}</span>"
        f"<span class='cf-title'>{title}</span>"
        f"<span class='cf-badge' style='background:{color}'>{band} · {int(vir.get('score', 0))}</span>"
        f"</div>"
        f"<div class='cf-meta'>{' · '.join(meta)}</div>"
        f"{_source_html(c)}"
        f"{_events_html(c)}"
        f"{('<div class=cf-flags>' + flag_html + '</div>') if flag_html else ''}"
        f"{details}"
        f"</div>")


def _cards_html(clips: list[dict]) -> str:
    """Ranked card gallery HTML for kept clips (Create results + History reopen)."""
    if not clips:
        return "<em>No clips.</em>"
    ranked = sorted(clips, key=lambda c: -(c.get("virality") or {}).get("score", 0))
    return "<div class='cf-gallery'>" + "".join(
        _clip_card(i, c) for i, c in enumerate(ranked, 1)) + "</div>"


APP_CSS = """
:root {
  --cf-accent:#6c5ce7; --cf-accent-ink:#ffffff;
  --cf-strong:#1a7f37; --cf-promising:#9a6700; --cf-weak:#b42318;
  --cf-radius:14px;
}
/* one accent across primary buttons, links, active tab */
.gradio-container .primary, .gradio-container button.primary {
  background:var(--cf-accent) !important; border-color:var(--cf-accent) !important;
  color:var(--cf-accent-ink) !important;
}
.gradio-container a { color:var(--cf-accent); }
/* tabs: clearly scannable, accent underline on the active one */
.gradio-container .tab-nav { gap:2px; }
.gradio-container .tab-nav button.selected {
  color:var(--cf-accent) !important; font-weight:700;
  border-bottom:3px solid var(--cf-accent) !important;
}
/* typography hierarchy for our own section headers */
.cf-section-title { font-size:1.02rem; font-weight:700; letter-spacing:.01em;
  margin:2px 0 2px; }
.cf-section-sub { font-size:.85rem; opacity:.65; margin:0 0 6px; }

/* ---- card gallery ---- */
.cf-gallery { display:flex; flex-direction:column; gap:12px; }
.cf-card { border:1px solid var(--border-color-primary,#d0d7de); border-radius:var(--cf-radius);
  padding:14px 16px; background:var(--background-fill-secondary,#fff);
  box-shadow:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.05); }
.cf-card-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.cf-rank { font-weight:700; opacity:.45; font-variant-numeric:tabular-nums; }
.cf-title { font-weight:650; flex:1; min-width:120px; }
.cf-badge { color:#fff; font-size:.78em; font-weight:700; padding:3px 11px; border-radius:999px; }
.cf-meta { margin-top:6px; font-size:.88em; opacity:.8; }
.cf-source { margin-top:4px; font-size:.82em; opacity:.6; font-variant-numeric:tabular-nums; }
.cf-flags { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
.cf-flag { font-size:.75em; background:#b4231822; color:#b42318; border-radius:6px; padding:2px 8px; }
.cf-details { margin-top:10px; font-size:.85em; }
.cf-details summary { cursor:pointer; opacity:.7; }
.cf-sig { display:grid; grid-template-columns:96px 90px 34px 1fr; gap:8px;
  align-items:center; margin:5px 0; }
.cf-sig-name { text-transform:capitalize; }
.cf-bar { background:#d0d7de55; border-radius:6px; height:8px; overflow:hidden; }
.cf-bar-fill { display:block; height:100%; background:var(--cf-accent); }
.cf-sig-score { text-align:right; font-variant-numeric:tabular-nums; }
.cf-sig-reason { opacity:.7; }

/* ---- popup / modal overlay (Gradio 6 has no gr.Modal) ---- */
.cf-modal { position:fixed !important; inset:0; z-index:1000;
  background:rgba(9,10,20,.62); padding:4vh 3vw; overflow:auto; }
.cf-modal > * { max-width:900px; margin:0 auto;
  background:var(--background-fill-primary,#fff); border-radius:var(--cf-radius);
  padding:18px 20px; box-shadow:0 20px 60px rgba(0,0,0,.4); }

/* ---- font gallery: vertical browse list, large real-render sample ---- */
.cf-font-row { padding:10px 4px; border-bottom:1px solid var(--border-color-primary,#e3e6ea); }
.cf-font-name { font-size:.78rem; letter-spacing:.02em; text-transform:uppercase;
  opacity:.55; margin-bottom:6px; }
.cf-font-row img { display:block; width:100%; max-width:760px; height:auto; border-radius:8px; }
"""


def _run_generator(file_path, url, preset, aspect, provider, n_clips, music,
                   music_vol, style_on=True, viral_on=True,
                   viral_upload=False, style_profile="default",
                   subs_mode="auto", cta_text="", highlight_hex="",
                   pacing="", clip_min="", clip_max="", watermark_text="",
                   watermark_pos="bottom-right", watermark_mode="text",
                   logo_path="", font_family=""):
    from config import apply_run_options
    from pipeline import run_job

    source = (url or "").strip() or file_path
    if not source:
        yield "Provide a video file or a URL.", {}, [], ""
        return
    music = _resolve_music_choice(music)
    target_count = int(n_clips) or None  # 0 = auto (keep-ratio rule)

    # Per-run options applied onto a private deep copy (never the singleton).
    try:
        cfg = apply_run_options(load_config(), {
            "cta_text": cta_text, "highlight_hex": highlight_hex,
            "preset": preset or None, "pacing": pacing,
            "clip_min": clip_min, "clip_max": clip_max,
            "watermark_text": watermark_text,
            "watermark_position": watermark_pos,
            "watermark_mode": watermark_mode,
            "watermark_image": _persist_branding(logo_path),
            "font_family": font_family})
    except Exception as e:  # noqa: BLE001 — a bad option must not crash the run
        yield f"Invalid option: {e}", {}, [], ""
        return
    if style_profile:  # point the refiner at the chosen profile
        cfg["style"]["profile"] = f"profiles/{style_profile}.json"
    # viral_v2 per-run toggles (allow_upload only ever set from an explicit
    # user click — config default stays false)
    cfg.setdefault("viral_v2", {})["enabled"] = bool(viral_on)
    cfg["viral_v2"]["allow_upload"] = bool(viral_upload)
    # Pacing only has a consumer inside the style refiner; say so honestly rather
    # than let the slider silently do nothing.
    if not style_on and str(pacing) not in ("", "0.5"):
        gr.Warning("Pacing aggressiveness only affects runs with Style "
                   "Refinement enabled — it was ignored this run.")
    q: queue.Queue = queue.Queue()
    holder: dict = {}

    import time
    from progress import _fmt_secs
    last = {"t": 0.0}
    t0 = time.monotonic()

    def cb(stage, frac, msg):
        now = time.monotonic()
        if now - last["t"] < 1.0 and 0.01 < frac < 0.99:
            return  # throttle: at most ~1 update/sec
        last["t"] = now
        filled = int(frac * 24)
        bar = "█" * filled + "░" * (24 - filled)
        el = now - t0
        eta = el * (1.0 - frac) / frac if frac > 0.03 else None
        q.put(f"{bar} {frac * 100:3.0f}%  {stage}: {msg} · ETA {_fmt_secs(eta)}")

    def work():
        try:
            holder["job"] = run_job(source, cfg, provider=provider or None,
                                    preset=preset or None,
                                    aspect=aspect or "9:16",
                                    target_count=target_count,
                                    music=music or None,
                                    music_volume_db=float(music_vol),
                                    progress_cb=cb,
                                    style_refine=bool(style_on),
                                    subs_mode=subs_mode or None)
        except Exception as e:  # noqa: BLE001 — UI must show, not crash
            holder["error"] = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    lines: list[str] = [f"Job started: {source}"]
    yield "\n".join(lines), {}, [], ""

    def _stage_of(s: str) -> str:
        return s.split("%", 1)[-1].split(":", 1)[0] if "%" in s else s

    while True:
        item = q.get()
        if item is None:
            break
        # progress-bar updates for the same stage replace the previous line
        # instead of flooding the log
        if (lines and lines[-1].startswith(("█", "░"))
                and item.startswith(("█", "░"))
                and _stage_of(lines[-1]) == _stage_of(item)):
            lines[-1] = item
        else:
            lines.append(item)
        yield "\n".join(lines[-25:]), {}, [], ""

    if "error" in holder:
        lines.append(f"FAILED: {holder['error']}")
        yield "\n".join(lines[-25:]), {}, [], ""
        return

    job = holder["job"]
    kept = [c for c in job["clips"] if c.get("kept")]
    files = [c["path"] for c in kept if Path(c["path"]).exists()]
    files += [c["srt"] for c in kept if Path(c.get("srt", "")).exists()]
    lines.append(f"Done: {len(kept)} clips kept of {len(job['clips'])} "
                 f"rendered → {job['job_dir']}")
    yield ("\n".join(lines[-25:]),
           {"job_dir": job["job_dir"], "clips": kept}, files, job["job_dir"])


# -------------------------------------------------------------- bulk download

def _zip_current(job_dir):
    from bundle import zip_job
    if not job_dir:
        return None
    try:
        return str(zip_job(job_dir))
    except Exception as e:  # noqa: BLE001
        log.warning("bundle failed: %s", e)
        return None


def _zip_history(job_id):
    from bundle import zip_job
    from history import get_job
    if not job_id:
        return None
    job = get_job(job_id.strip())
    if job is None or not job.get("job_dir"):
        return None
    try:
        return str(zip_job(job["job_dir"]))
    except Exception as e:  # noqa: BLE001
        log.warning("bundle failed: %s", e)
        return None


def _zip_batch_all():
    from batch import get_queue
    from bundle import zip_jobs
    dirs = [i["job_dir"] for i in get_queue().items
            if i["status"] == "done" and i.get("job_dir")]
    if not dirs:
        return None
    try:
        return str(zip_jobs(dirs))
    except Exception as e:  # noqa: BLE001
        log.warning("bundle failed: %s", e)
        return None


# ---------------------------------------------------------------- batch tab

def _batch_add(text, music_choice=""):
    from batch import get_queue
    q = get_queue()
    n = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # 'random' resolves per job here so every queued video can differ
        resolved = _resolve_music_choice(music_choice)
        q.add(line, **({"music": resolved} if resolved else {}))
        n += 1
    return f"Queued {n} source(s).", _batch_rows()


def _batch_rows():
    from batch import get_queue
    return get_queue().status_rows() or [["-", "queue empty", "-", "-"]]


def _inbox_toggle(enable):
    from batch import get_queue
    q = get_queue()
    return q.start_inbox_watcher() if enable else q.stop_inbox_watcher()


# ----------------------------------------------------------------- edit tab

def _list_job_dirs():
    cfg = load_config()
    out = ROOT / cfg["paths"]["output_dir"]
    if not out.exists():
        return []
    return sorted((str(d.name) for d in out.iterdir()
                   if d.is_dir() and (d / "job.json").exists()), reverse=True)


def _job_clips(job_name, desired=None):
    """Populate the clip dropdown for a job. When `desired` (a clip index) is
    given — set by a card's Edit button — pre-select that clip instead of the
    first; then clear it (3rd return) so later manual job changes behave
    normally. Returns (clip_dd update, info, cleared-desired)."""
    if not job_name:
        return gr.update(choices=[], value=None), "select a job", None
    choices, clips = _clip_choices_for(job_name)
    if not choices:
        return gr.update(choices=[], value=None), "no clips found", None
    sel, info = choices[0], f"{len(choices)} clips"
    if desired is not None:
        m = next((c for c in clips if int(c["index"]) == int(desired)), None)
        if m is not None:
            sel = f"{m['index']:02d} | {m['start']:.1f}-{m['end']:.1f}s | " \
                  f"{m['metadata']['title'][:50]}"
            info = f"Editing clip {int(desired):02d} of {job_name}"
    return gr.update(choices=choices, value=sel), info, None


def _font_upload(path, refresh):
    """Register an uploaded font; bump the refresh counter so the gallery
    @gr.render re-runs and shows it. Rejects non-fonts without crashing."""
    import fontreg
    try:
        info = fontreg.register_upload(path)
        return f"Added **{info['family']}**.", int(refresh or 0) + 1
    except Exception as e:  # noqa: BLE001 — a bad upload must not break the UI
        return f"Rejected: {e}", int(refresh or 0)


def _clip_choices_for(job_name: str):
    """(choice strings, clip records) for a job's clips, or ([], []) on error.
    Choice format matches _job_clips so the Edit dropdown stays consistent."""
    from rerender import load_job
    try:
        job = load_job(ROOT / load_config()["paths"]["output_dir"] / job_name)
    except Exception as e:  # noqa: BLE001 — missing/corrupt job → empty selector
        log.warning("could not load job %s: %s", job_name, e)
        return [], []
    clips = job["clips"]
    choices = [f"{c['index']:02d} | {c['start']:.1f}-{c['end']:.1f}s | "
               f"{c['metadata']['title'][:50]}" for c in clips]
    return choices, clips


def _open_edit_for(job_dir: str, index):
    """Jump to the Edit tab pre-loaded with a specific clip (from a card's Edit
    button). Sets the Job dropdown + start/end fields; the clip itself is
    pre-selected by _job_clips via the desired-clip state (set here) once the
    Job change fires. Returns [main_tabs, job_dd, desired_clip_state, start_in,
    end_in, edit_info]."""
    job_name = Path(job_dir).name if job_dir else ""
    _, clips = _clip_choices_for(job_name)
    match = next((c for c in clips if int(c["index"]) == int(index)), None)
    start = float(match["start"]) if match else 0.0
    end = float(match["end"]) if match else 45.0
    info = (f"Editing clip {int(index):02d} of {job_name}" if match
            else f"Could not locate clip {index} in {job_name}")
    # include the current job in the choices — a freshly-run job is not in the
    # dropdown's build-time list, so setting only its value would be rejected.
    jobs = _list_job_dirs()
    if job_name and job_name not in jobs:
        jobs = [job_name] + jobs
    return (gr.Tabs(selected="edit"),
            gr.update(choices=jobs, value=job_name),
            (int(index) if match else None), start, end, info)


def _edit_rerender(job_name, clip_choice, start, end, preset):
    """Streams live re-render progress (bar + ETA) then yields the final clip."""
    import queue as _queue
    import time
    from progress import ProgressTracker, _fmt_secs
    from rerender import rerender_clip
    if not (job_name and clip_choice):
        yield None, "select a job and a clip"
        return
    idx = int(clip_choice.split("|")[0].strip())
    job_dir = ROOT / load_config()["paths"]["output_dir"] / job_name
    q: _queue.Queue = _queue.Queue()
    holder: dict = {}
    t0 = time.monotonic()

    def on_change(tr):
        snap = tr.snapshot()
        row = next((s for s in snap["stages"] if s["key"] == "render"), None)
        if not row:
            return
        frac = row["fraction"]
        filled = int(frac * 24)
        bar = "█" * filled + "░" * (24 - filled)
        q.put(f"{bar} {frac * 100:3.0f}%  {row['message']} · "
              f"ETA {_fmt_secs(row['eta'])}")

    tracker = ProgressTracker(on_change=on_change)

    def work():
        try:
            holder["clip"] = rerender_clip(job_dir, idx, float(start),
                                           float(end), preset or None,
                                           tracker=tracker)
        except Exception as e:  # noqa: BLE001 — UI must show, not crash
            holder["error"] = str(e)
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    yield None, f"re-rendering clip {idx:02d}…"
    while True:
        item = q.get()
        if item is None:
            break
        yield None, item
    if "error" in holder:
        yield None, f"re-render failed: {holder['error']}"
        return
    clip = holder["clip"]
    yield clip["path"], (f"re-rendered clip {idx:02d} in "
                         f"{clip.get('render_s', 0):.0f}s: "
                         f"{clip['start']:.2f}–{clip['end']:.2f}s "
                         f"(snapped to sentence boundaries)")


def _edit_regen_meta(job_name, clip_choice):
    from rerender import regenerate_metadata
    if not (job_name and clip_choice):
        return "select a job and a clip"
    try:
        idx = int(clip_choice.split("|")[0].strip())
        job_dir = ROOT / load_config()["paths"]["output_dir"] / job_name
        meta = regenerate_metadata(job_dir, idx)
        return json.dumps(meta, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"metadata regeneration failed: {e}"


# --------------------------------------------------------------- upload tab

def _upload_status():
    import os
    import youtube_upload as yt
    if os.environ.get("CLIPFORGE_DEMO") == "1":
        return ("**YouTube upload is disabled in this hosted demo.** "
                "Run ClipForge locally (or in your own Space) to enable it — "
                "see the README.")
    if not yt.credentials_available():
        return ("**YouTube upload is not configured.**\n\n"
                + yt.SETUP_INSTRUCTIONS)
    if not yt.has_cached_token():
        return ("Client secrets found. Click **Authorize YouTube** to run the "
                "one-time browser authorization (uploads stay private by "
                "default).")
    return "YouTube is authorized. Select a job and upload kept clips."


def _yt_authorized() -> bool:
    import youtube_upload as yt
    try:
        return bool(yt.credentials_available() and yt.has_cached_token())
    except Exception:  # noqa: BLE001 — treat any probe failure as not-authorized
        return False


def _auto_panel_md() -> str:
    """Markdown snapshot of the auto-upload panel (state via panel_state)."""
    from upload_scheduler import load_log, panel_state
    try:
        st = panel_state(load_config(), load_log(), _yt_authorized())
    except Exception as e:  # noqa: BLE001 — panel must render even if log breaks
        log.warning("auto-upload panel state failed: %s", e)
        return "_Auto-upload status is unavailable right now._"
    lines = []
    if not st["authorized"]:
        lines.append("_Not connected to YouTube yet — click **Authorize "
                     "YouTube** above to connect (one-time)._")
    lines.append(f"Auto-upload is **{'ON' if st['auto_enabled'] else 'OFF'}** "
                 f"· uploaded today: **{st['uploads_today']} of "
                 f"{st['max_per_day']}**")
    if st["next_slot_ist"]:
        when = st["next_slot_ist"][:16].replace("T", " ")
        lines.append(f"Next publish slot: **{when} IST**")
    if st["recent"]:
        lines.append("**Recent scheduled uploads:**")
        for r in st["recent"]:
            when = (r["publish_at"] or "")[:16].replace("T", " ")
            name = r["title"] or r["video_id"]
            lines.append(f"- [{name}]({r['url']}) — goes live {when} IST")
    else:
        lines.append("_No uploads yet. Once enabled and authorized, clips "
                     "that score well are scheduled automatically as they "
                     "finish rendering._")
    return "\n\n".join(lines)


def _set_auto_upload(enabled):
    from config import save_config
    try:
        save_config({"upload": {"auto_enabled": bool(enabled)}})
    except Exception as e:  # noqa: BLE001
        log.warning("could not save auto-upload setting: %s", e)
    return _auto_panel_md()


def _clip_excluded(clip_path: str) -> bool:
    try:
        meta = json.loads((Path(clip_path).parent / "metadata.json")
                          .read_text(encoding="utf-8"))
        return bool(meta.get("upload", {}).get("exclude"))
    except Exception:  # noqa: BLE001 — missing/old metadata → not excluded
        return False


def _set_clip_exclude(clip_path: str, exclude: bool) -> None:
    """Persist the per-clip auto-upload opt-out where find_candidates reads it."""
    try:
        meta_path = Path(clip_path).parent / "metadata.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.setdefault("upload", {})["exclude"] = bool(exclude)
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — a failed toggle must not crash UI
        log.warning("could not update auto-upload opt-out: %s", e)


def _authorize():
    import youtube_upload as yt
    try:
        if not yt.credentials_available():
            return _upload_status()
        yt.authorize()
        return _upload_status()
    except Exception as e:  # noqa: BLE001
        return f"authorization failed: {e}"


def _upload_job(job_name, privacy):
    import youtube_upload as yt
    from rerender import load_job
    if not job_name:
        return "select a job"
    if not yt.credentials_available() or not yt.has_cached_token():
        return _upload_status()
    try:
        job = load_job(ROOT / load_config()["paths"]["output_dir"] / job_name)
        results = []
        for c in job["clips"]:
            if not c.get("kept"):
                continue
            r = yt.upload_clip(c["path"], c["metadata"], privacy=privacy)
            results.append(f"- {c['metadata']['title']} → {r['url']}")
        return "Uploaded:\n" + "\n".join(results) if results else "no kept clips"
    except Exception as e:  # noqa: BLE001 — includes friendly quota message
        return f"upload stopped: {e}"


# -------------------------------------------------------------- history tab

def _history_rows():
    from history import get_job, list_jobs
    rows = []
    for j in list_jobs():
        total = ""
        full = get_job(j["job_id"])
        if full:
            secs = sum(float(s.get("seconds", 0) or 0)
                       for s in (full.get("stages") or {}).values())
            total = f"{secs:.0f}s"
        rows.append([j["created"], j["job_id"], j["source"][-50:], j["status"],
                     f"{j['kept']}/{j['clip_count']}", total])
    return rows or [["-", "-", "no jobs yet", "-", "-", "-"]]


def _history_open(job_id):
    from history import get_job
    if not job_id:
        return [], "enter a job id from the table", ""
    job = get_job(job_id.strip())
    if job is None:
        return [], f"job '{job_id}' not found", ""
    kept = [c for c in job["clips"] if c.get("kept")]
    files = [c["path"] for c in kept if Path(c["path"]).exists()]
    return (files, f"{len(files)} downloadable clips from {job['job_dir']}",
            _cards_html(kept))


# ------------------------------------------------------------- settings tab

def _detected_hardware() -> str:
    import os

    from ffutil import nvenc_available
    from transcribe import gpu_available
    gpu = gpu_available()
    nvenc = nvenc_available()
    return (f"**Detected hardware** — CUDA GPU (Whisper): "
            f"{'yes' if gpu else 'no'} · NVENC (encoder): "
            f"{'yes' if nvenc else 'no'} · CPU cores: {os.cpu_count()}")


def _save_settings(compute, whisper_model, provider, gemini_model, groq_model,
                   ollama_model):
    from config import save_config
    try:
        save_config({
            "render": {"compute": compute},
            "whisper": {"model_override": whisper_model or ""},
            "llm": {"provider": provider,
                    "gemini_model": gemini_model.strip(),
                    "groq_model": groq_model.strip(),
                    "ollama_model": ollama_model.strip()},
        })
        return (f"Saved. Compute = **{compute}**, Whisper model = "
                f"**{whisper_model or 'matrix default'}**, LLM = "
                f"**{provider}**.\n\n" + _detected_hardware())
    except Exception as e:  # noqa: BLE001
        return f"save failed: {e}"


# ------------------------------------------------------------------- layout

def _update_banner() -> str:
    import updater
    s = updater.get_state()
    v = updater.current_version()
    if not s.get("checked"):
        return f"ClipForge v{v}"
    if s.get("update_available"):
        return (f"ClipForge v{v} — **update available: {s['latest']}**. "
                "Click *Install update* (your settings, models and videos "
                "are preserved).")
    if s.get("error"):
        return f"ClipForge v{v} (update check: {s['error']})"
    return f"ClipForge v{v} — up to date"


def _do_update():
    import updater
    try:
        return updater.apply_update(), _update_banner()
    except Exception as e:  # noqa: BLE001 — updater rolls back; UI must show why
        return f"Update failed (no changes were kept): {e}", _update_banner()


def build_app() -> gr.Blocks:
    import updater
    updater.check_async()
    cfg = load_config()
    presets = list(cfg["captions"]["presets"].keys())
    with gr.Blocks(title="ClipForge") as demo:
        gr.Markdown("# ClipForge — local video repurposing\n"
                    "Long video in → ranked vertical clips with animated "
                    "captions out. No API key required (mock provider); add "
                    "GEMINI_API_KEY in .env for LLM-powered selection.")
        with gr.Row():
            update_md = gr.Markdown(_update_banner())
            with gr.Column(scale=0, min_width=180):
                update_check_btn = gr.Button("Check for updates", size="sm")
                update_btn = gr.Button("Install update", size="sm",
                                       variant="primary")
        update_result = gr.Markdown()
        update_check_btn.click(
            lambda: (__import__("updater").check_for_update()
                     and _update_banner()) or _update_banner(),
            [], [update_md])
        update_btn.click(_do_update, [], [update_result, update_md])
        # carries the clip a card's Edit button wants pre-selected, honored by
        # _job_clips when the Job dropdown's change fires (avoids the change
        # handler clobbering the selection with the first clip).
        desired_clip_state = gr.State(None)
        with gr.Tabs() as main_tabs:
            with gr.Tab("Create", id="create"):
                with gr.Row():
                    with gr.Column(scale=1):
                        file_in = gr.File(label="Video file", type="filepath",
                                          file_types=["video"])
                        url_in = gr.Textbox(label="…or video URL (yt-dlp)")
                        preset_state = gr.State(cfg["captions"]["preset"])
                        preset_label = gr.Markdown(
                            f"**Caption style:** {cfg['captions']['preset']}")
                        preset_btn = gr.Button("Choose caption style", size="sm")
                        aspect_state = gr.State("9:16")
                        aspect_label = gr.Markdown(
                            "**Output shape:** Vertical (9:16)")
                        aspect_btn = gr.Button("Choose output shape", size="sm")
                        clips_in = gr.Slider(
                            0, 20, value=int(cfg["clips"].get("target_count", 0)),
                            step=1, label="Clips to keep (0 = auto)")
                        music_state = gr.State("")
                        music_label = gr.Markdown("**Background music:** None")
                        music_btn = gr.Button("Choose background music",
                                              size="sm")
                        music_vol = gr.Slider(-40, 0, value=-22, step=1,
                                              label="Music volume (dB)")
                        provider_in = gr.Dropdown(
                            ["", "mock", "gemini", "groq", "ollama",
                             "openrouter"], value="",
                            label="LLM provider override")
                        style_in = gr.Checkbox(
                            value=bool(cfg.get("style", {}).get("enabled", True)),
                            label="Style refinement (hooks, pacing, endings, captions)")
                        viral_in = gr.Checkbox(
                            value=bool(cfg.get("viral_v2", {}).get("enabled", True)),
                            label="Viral detection v2 (laughter, reactions, falls "
                                  "— video AI + audio analysis)")
                        viral_upload_in = gr.Checkbox(
                            value=bool(cfg.get("viral_v2", {}).get("allow_upload", False)),
                            label="Analyze LOCAL video files too — sends video "
                                  "content to the AI provider for analysis "
                                  "(YouTube URLs are always analyzed; audio "
                                  "analysis stays local either way)")
                        _default_profile = (
                            cfg.get("style", {}).get("profile", "profiles/default.json")
                            .split("/")[-1].removesuffix(".json"))
                        profile_state = gr.State(_default_profile)
                        profile_label = gr.Markdown(
                            f"**Style profile:** {_default_profile}")
                        profile_btn = gr.Button("Choose style profile", size="sm")
                        subs_state = gr.State("auto")
                        subs_label = gr.Markdown(
                            "**Existing subtitles:** Decide for me")
                        subs_btn = gr.Button(
                            "Existing subtitles in the video…", size="sm")
                        with gr.Accordion("Style & Branding", open=False):
                            gr.HTML("<div class='cf-section-title'>Captions</div>"
                                    "<div class='cf-section-sub'>call-to-action "
                                    "and keyword highlight colour</div>")
                            cta_in = gr.Textbox(
                                label="Custom CTA text (blank = config default)",
                                placeholder=cfg.get("style", {}).get("cta", {})
                                .get("text", "Follow for more"))
                            highlight_in = gr.ColorPicker(
                                label="Keyword highlight color (blank = preset default)",
                                value="")
                            gr.HTML("<div class='cf-section-title'>Timing</div>"
                                    "<div class='cf-section-sub'>pacing and clip "
                                    "length</div>")
                            pacing_in = gr.Slider(
                                0, 1, value=0.5, step=0.05,
                                label="Pacing aggressiveness (0 gentle → 1 tight cuts) "
                                      "· needs Style Refinement")
                            with gr.Row():
                                clip_min_in = gr.Number(
                                    value=int(cfg["clips"]["min_seconds"]),
                                    label="Min clip length (s)", precision=0)
                                clip_max_in = gr.Number(
                                    value=int(cfg["clips"]["max_seconds"]),
                                    label="Max clip length (s)", precision=0)
                            gr.HTML("<div class='cf-section-title'>Branding</div>"
                                    "<div class='cf-section-sub'>watermark text or "
                                    "a logo overlay</div>")
                            watermark_mode_in = gr.Radio(
                                ["text", "image", "off"], value="text",
                                label="Watermark mode (image = logo overlay)")
                            watermark_in = gr.Textbox(
                                label="Watermark / brand text (blank = off)",
                                placeholder="@yourhandle")
                            wmpos_state = gr.State("bottom-right")
                            wmpos_label = gr.Markdown(
                                "**Watermark position:** Bottom right")
                            wmpos_btn = gr.Button("Choose watermark position",
                                                  size="sm")
                            logo_in = gr.Image(
                                type="filepath", height=90,
                                label="Logo PNG (used when mode = image)")
                            gr.HTML("<div class='cf-section-title'>Fonts</div>"
                                    "<div class='cf-section-sub'>preview each font "
                                    "through the real caption burn, then pick one</div>")
                            font_label = gr.Markdown("**Font:** preset default")
                            browse_fonts_btn = gr.Button("Browse fonts", size="sm")
                        font_override_state = gr.State("")
                        run_btn = gr.Button("Create clips", variant="primary")
                    with gr.Column(scale=2):
                        progress_out = gr.Textbox(label="Progress", lines=10)
                        results_state = gr.State({})

                        @gr.render(inputs=[results_state])
                        def _render_results(res):
                            clips = (res or {}).get("clips") or []
                            job_dir = (res or {}).get("job_dir") or ""
                            if not clips:
                                gr.HTML("<em>Ranked clips appear here after a "
                                        "run.</em>")
                                return
                            ranked = sorted(clips, key=lambda c:
                                            -(c.get("virality") or {}).get("score", 0))
                            for i, c in enumerate(ranked, 1):
                                with gr.Column():
                                    gr.HTML(_clip_card(i, c))
                                    excl = gr.Checkbox(
                                        value=_clip_excluded(c.get("path", "")),
                                        label="Don't auto-upload this clip")
                                    excl.change(
                                        (lambda v, p=c.get("path", ""):
                                         _set_clip_exclude(p, v)),
                                        [excl], None)
                                    eb = gr.Button("Edit this clip", size="sm")
                                    eb.click(
                                        (lambda jd=job_dir, ix=c.get("index"):
                                         _open_edit_for(jd, ix)),
                                        None,
                                        [main_tabs, job_dd, desired_clip_state,
                                         start_in, end_in, edit_info])
                        files_out = gr.Files(label="Download clips + subtitles")
                        job_dir_state = gr.State("")
                        with gr.Row():
                            zip_btn = gr.Button("Download all (zip)")
                        zip_out = gr.File(label="Bundle (.zip)")
                run_btn.click(_run_generator,
                              [file_in, url_in, preset_state, aspect_state,
                               provider_in,
                               clips_in, music_state, music_vol,
                               style_in, viral_in, viral_upload_in,
                               profile_state, subs_state,
                               cta_in, highlight_in, pacing_in,
                               clip_min_in, clip_max_in,
                               watermark_in, wmpos_state,
                               watermark_mode_in, logo_in, font_override_state],
                              [progress_out, results_state, files_out, job_dir_state])
                zip_btn.click(_zip_current, [job_dir_state], [zip_out])

                # ---- font gallery popup (Gradio 6 has no gr.Modal) ----
                font_refresh_state = gr.State(0)
                with gr.Column(visible=False,
                               elem_classes=["cf-modal"]) as font_modal:
                    gr.Markdown("### Browse fonts\nEach sample is rendered "
                                "through the real caption burn for the active "
                                "preset. Pick one to use on this run.")
                    font_upload = gr.File(label="Upload a font (.ttf / .otf)",
                                          file_types=[".ttf", ".otf"])
                    font_status = gr.Markdown()
                    close_fonts_btn = gr.Button("Close")

                    @gr.render(inputs=[preset_state, font_refresh_state])
                    def _font_gallery(preset_name, _refresh):
                        import fontreg
                        import style_preview
                        cfg2 = load_config()
                        pname = preset_name or cfg2["captions"]["preset"]
                        fonts = fontreg.list_fonts(cfg2)
                        if not fonts:
                            gr.Markdown("_No fonts found._")
                            return
                        for f in fonts:
                            fam = f["family"]
                            try:
                                png = style_preview.preview_png(pname, fam, cfg=cfg2)
                            except Exception as e:  # noqa: BLE001 — skip a bad font
                                log.warning("preview failed for %s: %s", fam, e)
                                continue
                            with gr.Column(elem_classes=["cf-font-row"]):
                                src = f"/gradio_api/file={png.resolve().as_posix()}"
                                gr.HTML(f"<div class='cf-font-name'>"
                                        f"{_esc_html(fam)} · {f['source']}</div>"
                                        f"<img src='{src}' alt='{_esc_html(fam)}'>")
                                pick = gr.Button(f"Use {fam}", size="sm")
                                pick.click(
                                    (lambda family=fam:
                                     (family, f"**Font:** {family}",
                                      gr.update(visible=False))),
                                    None,
                                    [font_override_state, font_label, font_modal])

                browse_fonts_btn.click(lambda: gr.update(visible=True), None,
                                       [font_modal])
                close_fonts_btn.click(lambda: gr.update(visible=False), None,
                                      [font_modal])
                font_upload.upload(_font_upload,
                                   [font_upload, font_refresh_state],
                                   [font_status, font_refresh_state])

                # ---- popup card-pickers (same overlay pattern as fonts) ----
                with gr.Column(visible=False,
                               elem_classes=["cf-modal"]) as preset_modal:
                    gr.Markdown("### Choose a caption style\nEach sample is "
                                "rendered through the real caption burn.")
                    close_preset_btn = gr.Button("Close")

                    @gr.render(inputs=[font_override_state, font_refresh_state])
                    def _preset_gallery(font_fam, _refresh):
                        import style_preview
                        cfg2 = load_config()
                        for pname in cfg2["captions"]["presets"]:
                            try:
                                png = style_preview.preview_png(
                                    pname, font_fam or None, cfg=cfg2)
                            except Exception as e:  # noqa: BLE001 — skip bad preset
                                log.warning("preview failed for preset %s: %s",
                                            pname, e)
                                continue
                            with gr.Column(elem_classes=["cf-font-row"]):
                                src = f"/gradio_api/file={png.resolve().as_posix()}"
                                gr.HTML(f"<div class='cf-font-name'>"
                                        f"{_esc_html(pname)}</div>"
                                        f"<img src='{src}' alt='{_esc_html(pname)}'>")
                                pick = gr.Button(f"Use: {pname}", size="sm")
                                pick.click(
                                    (lambda p=pname:
                                     (p, f"**Caption style:** {p}",
                                      gr.update(visible=False))),
                                    None,
                                    [preset_state, preset_label, preset_modal])

                close_preset_btn.click(lambda: gr.update(visible=False), None,
                                       [preset_modal])
                preset_btn.click(lambda: gr.update(visible=True), None,
                                 [preset_modal])

                aspect_modal = _static_picker_modal(
                    "Choose the output shape",
                    "Pick where you'll post these clips.",
                    [(v, f"{_ASPECT_CARDS[v][0]} ({v})", _aspect_card_html(v))
                     for v in _ASPECT_CARDS],
                    aspect_state, aspect_label, "Output shape")
                aspect_btn.click(lambda: gr.update(visible=True), None,
                                 [aspect_modal])

                subs_modal = _static_picker_modal(
                    "Existing subtitles in the video",
                    "What should happen when the source video already has "
                    "subtitles burned into the picture?",
                    [(v, _SUBS_CARDS[v][0], _subs_card_html(v))
                     for v in _SUBS_CARDS],
                    subs_state, subs_label, "Existing subtitles")
                subs_btn.click(lambda: gr.update(visible=True), None,
                               [subs_modal])

                wmpos_modal = _static_picker_modal(
                    "Choose the watermark position",
                    "Where your watermark text or logo sits on the clip.",
                    [(v, _WMPOS_CARDS[v], _wmpos_card_html(v))
                     for v in _WMPOS_CARDS],
                    wmpos_state, wmpos_label, "Watermark position")
                wmpos_btn.click(lambda: gr.update(visible=True), None,
                                [wmpos_modal])

                _profile_cards = []
                for _pn in _profile_choices():
                    try:
                        _pdata = json.loads(
                            (ROOT / "profiles" / f"{_pn}.json")
                            .read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001 — card still renders
                        _pdata = {}
                    _profile_cards.append((_pn, _pn,
                                           _profile_card_html(_pn, _pdata)))
                profile_modal = _static_picker_modal(
                    "Choose a style profile",
                    "A style profile is a look learned from reference videos "
                    "(colors, pacing, hooks). 'default' is ClipForge's "
                    "built-in style.",
                    _profile_cards, profile_state, profile_label,
                    "Style profile")
                profile_btn.click(lambda: gr.update(visible=True), None,
                                  [profile_modal])

                # ---- music picker: preview-first gallery ----
                with gr.Column(visible=False,
                               elem_classes=["cf-modal"]) as music_modal:
                    gr.Markdown(
                        "### Choose background music\nTracks are copyright-"
                        "free; a credit line is added to the video "
                        "description automatically when the license asks for "
                        "one. Music is gently lowered whenever someone "
                        "speaks.")
                    close_music_btn = gr.Button("Close")
                    music_preview_audio = gr.Audio(
                        label="Preview player", interactive=False)
                    _music_specials = [
                        ("", "No music", "Keep the clip's own audio only"),
                        ("auto", "Match the video",
                         "Picks the track whose mood best fits what's said "
                         "in the video"),
                        ("random", "Surprise me",
                         "A random track per clip batch — nice for variety "
                         "across many uploads"),
                    ]
                    for _mv, _md, _mdesc in _music_specials:
                        with gr.Column(elem_classes=["cf-font-row"]):
                            gr.HTML(f"<div><b>{_md}</b><br><span "
                                    f"style='opacity:.7'>{_mdesc}</span></div>")
                            _mb = gr.Button(f"Use: {_md}", size="sm")
                            _mb.click(
                                (lambda v=_mv, d=_md:
                                 (v, f"**Background music:** {d}",
                                  gr.update(visible=False))),
                                None,
                                [music_state, music_label, music_modal])
                    try:
                        import music as _music_mod
                        _tracks = _music_mod.list_tracks()
                    except Exception as e:  # noqa: BLE001 — manifest optional
                        log.warning("music manifest unavailable: %s", e)
                        _tracks = []
                    for _t in _tracks:
                        with gr.Column(elem_classes=["cf-font-row"]):
                            gr.HTML(_music_card_html(_t))
                            with gr.Row():
                                _pv = gr.Button("Preview", size="sm")
                                _use = gr.Button(
                                    f"Use: {_t.get('title', _t['id'])}",
                                    size="sm")
                            _pv.click((lambda tid=_t["id"]:
                                       _music_preview(tid)),
                                      None, [music_preview_audio])
                            _use.click(
                                (lambda tid=_t["id"],
                                        d=_t.get("title", _t["id"]):
                                 (tid, f"**Background music:** {d}",
                                  gr.update(visible=False))),
                                None,
                                [music_state, music_label, music_modal])
                close_music_btn.click(lambda: gr.update(visible=False), None,
                                      [music_modal])
                music_btn.click(lambda: gr.update(visible=True), None,
                                [music_modal])

            with gr.Tab("Batch", id="batch"):
                batch_in = gr.Textbox(label="One file path or URL per line",
                                      lines=5)
                batch_music_in = gr.Dropdown(
                    [("No music", ""), ("Match each video", "auto"),
                     ("Random track per video", "random")] +
                    [(t.get("title", t["id"]), t["id"]) for t in _tracks],
                    value="", label="Background music for these videos")
                with gr.Row():
                    batch_btn = gr.Button("Add to queue", variant="primary")
                    refresh_btn = gr.Button("Refresh status")
                    inbox_cb = gr.Checkbox(label=f"Watch {cfg['paths']['inbox_dir']}/ "
                                                 "folder for new videos")
                batch_msg = gr.Markdown()
                batch_table = gr.Dataframe(
                    headers=["id", "source", "status", "message"],
                    interactive=False)
                with gr.Row():
                    batch_zip_btn = gr.Button("Download everything (zip)")
                batch_zip_out = gr.File(label="All jobs bundle (.zip)")
                batch_btn.click(_batch_add, [batch_in, batch_music_in],
                                [batch_msg, batch_table])
                refresh_btn.click(_batch_rows, [], [batch_table])
                inbox_cb.change(_inbox_toggle, [inbox_cb], [batch_msg])
                batch_zip_btn.click(_zip_batch_all, [], [batch_zip_out])

            with gr.Tab("Edit clips", id="edit"):
                with gr.Row():
                    job_dd = gr.Dropdown(_list_job_dirs(), label="Job")
                    jobs_refresh = gr.Button("Refresh jobs")
                clip_dd = gr.Dropdown([], label="Clip")
                edit_info = gr.Markdown()
                with gr.Row():
                    start_in = gr.Number(label="New start (s)", value=0.0)
                    end_in = gr.Number(label="New end (s)", value=45.0)
                    preset_edit = gr.Dropdown([""] + presets,
                                              label="Preset (blank = keep)")
                with gr.Row():
                    rerender_btn = gr.Button("Re-render this clip",
                                             variant="primary")
                    regen_btn = gr.Button("Regenerate metadata")
                edit_video = gr.Video(label="Re-rendered clip")
                edit_meta = gr.Code(label="Metadata", language="json")
                jobs_refresh.click(lambda: gr.update(choices=_list_job_dirs()),
                                   [], [job_dd])
                job_dd.change(_job_clips, [job_dd, desired_clip_state],
                              [clip_dd, edit_info, desired_clip_state])
                rerender_btn.click(_edit_rerender,
                                   [job_dd, clip_dd, start_in, end_in, preset_edit],
                                   [edit_video, edit_info])
                regen_btn.click(_edit_regen_meta, [job_dd, clip_dd], [edit_meta])

            with gr.Tab("YouTube upload", id="youtube"):
                upload_md = gr.Markdown(_upload_status())
                with gr.Row():
                    auth_btn = gr.Button("Authorize YouTube")
                    upload_job_dd = gr.Dropdown(_list_job_dirs(), label="Job")
                    privacy_dd = gr.Dropdown(["private", "unlisted", "public"],
                                             value="private", label="Privacy")
                    upload_btn = gr.Button("Upload kept clips", variant="primary")
                upload_result = gr.Markdown()
                gr.HTML("<div class='cf-section-title'>Automatic uploads</div>"
                        "<div class='cf-section-sub'>schedule your best clips "
                        "to YouTube as soon as they finish rendering</div>")
                auto_enabled_cb = gr.Checkbox(
                    value=bool(cfg.get("upload", {}).get("auto_enabled",
                                                         False)),
                    label="Upload my best clips automatically")
                auto_panel_md = gr.Markdown(_auto_panel_md())
                auto_refresh_btn = gr.Button("Refresh status", size="sm")
                auto_enabled_cb.change(_set_auto_upload, [auto_enabled_cb],
                                       [auto_panel_md])
                auto_refresh_btn.click(_auto_panel_md, [], [auto_panel_md])
                auth_btn.click(_authorize, [], [upload_md]).then(
                    _auto_panel_md, [], [auto_panel_md])
                upload_btn.click(_upload_job, [upload_job_dd, privacy_dd],
                                 [upload_result])

            with gr.Tab("History", id="history"):
                with gr.Row():
                    hist_refresh = gr.Button("Refresh")
                    hist_id = gr.Textbox(label="Job id to reopen")
                    hist_open = gr.Button("Open job")
                hist_table = gr.Dataframe(
                    headers=["created", "job_id", "source", "status", "kept",
                             "total time"],
                    value=_history_rows(), interactive=False)
                hist_msg = gr.Markdown()
                hist_cards = gr.HTML()
                hist_files = gr.Files(label="Clips")
                hist_zip_btn = gr.Button("Download all (zip)")
                hist_zip_out = gr.File(label="Bundle (.zip)")
                hist_refresh.click(_history_rows, [], [hist_table])
                hist_open.click(_history_open, [hist_id],
                                [hist_files, hist_msg, hist_cards])
                hist_zip_btn.click(_zip_history, [hist_id], [hist_zip_out])

            with gr.Tab("Settings", id="settings"):
                hw_md = gr.Markdown(_detected_hardware())
                compute_in = gr.Radio(["auto", "gpu", "cpu"],
                                      value=cfg["render"].get("compute", "auto"),
                                      label="Compute (Whisper device + encoder)")
                whisper_model_in = gr.Dropdown(
                    ["", "tiny", "base", "small", "medium", "large-v3"],
                    value=cfg["whisper"].get("model_override", ""),
                    label="Whisper model (blank = matrix default)")
                provider_set = gr.Dropdown(
                    ["auto", "mock", "gemini", "groq", "ollama"],
                    value=cfg["llm"].get("provider", "auto"),
                    label="LLM provider")
                gemini_model_in = gr.Textbox(value=cfg["llm"].get("gemini_model", ""),
                                             label="Gemini model")
                groq_model_in = gr.Textbox(value=cfg["llm"].get("groq_model", ""),
                                           label="Groq model")
                ollama_model_in = gr.Textbox(value=cfg["llm"].get("ollama_model", ""),
                                             label="Ollama model")
                save_btn = gr.Button("Save settings", variant="primary")
                settings_status = gr.Markdown()
                save_btn.click(_save_settings,
                               [compute_in, whisper_model_in, provider_set,
                                gemini_model_in, groq_model_in, ollama_model_in],
                               [settings_status])
    return demo


if __name__ == "__main__":
    import os
    port = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    host = os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1")  # 0.0.0.0 in Docker
    # Auto-open the UI per config (ui.auto_open / ui.window_mode). A background
    # poller waits for the server, then launches a chromeless app window (Edge/
    # Chrome) or falls back to a browser tab. Docker (0.0.0.0) skips auto-open.
    if host != "0.0.0.0":
        from launcher import open_ui
        open_ui(f"http://127.0.0.1:{port}", load_config())
    # allow serving generated font previews + branding/font assets as <img> src
    allowed = [str(ROOT / "cache" / "font_previews"), str(ROOT / "assets")]
    build_app().queue().launch(server_name=host, server_port=port,
                               inbrowser=False, quiet=True,
                               theme=gr.themes.Soft(primary_hue="violet"),
                               css=APP_CSS, allowed_paths=allowed)
