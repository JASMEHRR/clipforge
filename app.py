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

def _music_choices():
    """(label, value) pairs for the music dropdown: None, Auto-match, tracks."""
    choices = [("None", ""), ("Auto-match (from transcript)", "auto")]
    try:
        import music
        choices += [(f"{t['title']} ({t['license']})", t["id"])
                    for t in music.list_tracks()]
    except Exception as e:  # noqa: BLE001 — manifest optional; UI still loads
        log.warning("music manifest unavailable: %s", e)
    return choices


def _profile_choices():
    """Stems of profiles/*.json for the style-profile dropdown."""
    from config import ROOT
    pdir = ROOT / "profiles"
    return sorted(p.stem for p in pdir.glob("*.json")) if pdir.exists() else ["default"]


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


CARD_CSS = """
.cf-gallery { display:flex; flex-direction:column; gap:12px; }
.cf-card { border:1px solid var(--border-color-primary,#d0d7de); border-radius:12px;
  padding:14px 16px; background:var(--background-fill-secondary,#fff); }
.cf-card-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.cf-rank { font-weight:700; opacity:.5; }
.cf-title { font-weight:600; flex:1; min-width:120px; }
.cf-badge { color:#fff; font-size:.8em; font-weight:700; padding:2px 10px; border-radius:999px; }
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
.cf-bar-fill { display:block; height:100%; background:#2563eb; }
.cf-sig-score { text-align:right; font-variant-numeric:tabular-nums; }
.cf-sig-reason { opacity:.7; }
"""


def _run_generator(file_path, url, preset, aspect, provider, n_clips, music,
                   music_vol, style_on=True, style_profile="default",
                   subs_mode="auto", cta_text="", highlight_hex="",
                   pacing="", clip_min="", clip_max="", watermark_text="",
                   watermark_pos="bottom-right", watermark_mode="text",
                   logo_path=""):
    from config import apply_run_options
    from pipeline import run_job

    source = (url or "").strip() or file_path
    if not source:
        yield "Provide a video file or a URL.", "", [], ""
        return
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
            "watermark_image": _persist_branding(logo_path)})
    except Exception as e:  # noqa: BLE001 — a bad option must not crash the run
        yield f"Invalid option: {e}", "", [], ""
        return
    if style_profile:  # point the refiner at the chosen profile
        cfg["style"]["profile"] = f"profiles/{style_profile}.json"
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
    yield "\n".join(lines), "", [], ""

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
        yield "\n".join(lines[-25:]), "", [], ""

    if "error" in holder:
        lines.append(f"FAILED: {holder['error']}")
        yield "\n".join(lines[-25:]), "", [], ""
        return

    job = holder["job"]
    kept = [c for c in job["clips"] if c.get("kept")]
    cards = _cards_html(kept)
    files = [c["path"] for c in kept if Path(c["path"]).exists()]
    files += [c["srt"] for c in kept if Path(c.get("srt", "")).exists()]
    lines.append(f"Done: {len(kept)} clips kept of {len(job['clips'])} "
                 f"rendered → {job['job_dir']}")
    yield "\n".join(lines[-25:]), cards, files, job["job_dir"]


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

def _batch_add(text):
    from batch import get_queue
    n = get_queue().add_many(text)
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


def _job_clips(job_name):
    if not job_name:
        return gr.update(choices=[]), "select a job"
    from rerender import load_job
    try:
        job = load_job(ROOT / load_config()["paths"]["output_dir"] / job_name)
        choices = [f"{c['index']:02d} | {c['start']:.1f}-{c['end']:.1f}s | "
                   f"{c['metadata']['title'][:50]}" for c in job["clips"]]
        return gr.update(choices=choices, value=choices[0] if choices else None), \
            f"{len(choices)} clips"
    except Exception as e:  # noqa: BLE001
        return gr.update(choices=[]), f"error: {e}"


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
        gr.HTML(f"<style>{CARD_CSS}</style>")  # scoped card styling (version-proof)
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
        with gr.Tab("Create"):
            with gr.Row():
                with gr.Column(scale=1):
                    file_in = gr.File(label="Video file", type="filepath",
                                      file_types=["video"])
                    url_in = gr.Textbox(label="…or video URL (yt-dlp)")
                    preset_in = gr.Dropdown(presets, label="Caption preset",
                                            value=cfg["captions"]["preset"])
                    aspect_in = gr.Radio(["9:16", "1:1", "16:9"], value="9:16",
                                         label="Output aspect")
                    clips_in = gr.Slider(
                        0, 20, value=int(cfg["clips"].get("target_count", 0)),
                        step=1, label="Clips to keep (0 = auto)")
                    music_in = gr.Dropdown(
                        _music_choices(), value="",
                        label="Background music (CC-licensed)")
                    music_vol = gr.Slider(-40, 0, value=-22, step=1,
                                          label="Music volume (dB)")
                    provider_in = gr.Dropdown(
                        ["", "mock", "gemini", "groq", "ollama"], value="",
                        label="LLM provider override")
                    style_in = gr.Checkbox(
                        value=bool(cfg.get("style", {}).get("enabled", True)),
                        label="Style refinement (hooks, pacing, endings, captions)")
                    profile_in = gr.Dropdown(
                        _profile_choices(),
                        value=(cfg.get("style", {}).get("profile", "profiles/default.json")
                               .split("/")[-1].removesuffix(".json")),
                        label="Style profile")
                    subs_in = gr.Dropdown(
                        ["auto", "replace", "keep", "ignore"], value="auto",
                        label="Burned-in subtitles")
                    with gr.Accordion("More options", open=False):
                        cta_in = gr.Textbox(
                            label="Custom CTA text (blank = config default)",
                            placeholder=cfg.get("style", {}).get("cta", {})
                            .get("text", "Follow for more"))
                        highlight_in = gr.ColorPicker(
                            label="Keyword highlight color (blank = preset default)",
                            value="")
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
                        watermark_in = gr.Textbox(
                            label="Watermark / brand text (blank = off)",
                            placeholder="@yourhandle")
                        watermark_pos_in = gr.Dropdown(
                            ["top-left", "top-right", "bottom-left",
                             "bottom-right", "center"], value="bottom-right",
                            label="Watermark position")
                        watermark_mode_in = gr.Radio(
                            ["text", "image", "off"], value="text",
                            label="Watermark mode (image = logo overlay)")
                        logo_in = gr.Image(
                            type="filepath", height=90,
                            label="Logo PNG (used when mode = image)")
                    run_btn = gr.Button("Create clips", variant="primary")
                with gr.Column(scale=2):
                    progress_out = gr.Textbox(label="Progress", lines=10)
                    ranking_out = gr.HTML()
                    files_out = gr.Files(label="Download clips + subtitles")
                    job_dir_state = gr.State("")
                    with gr.Row():
                        zip_btn = gr.Button("Download all (zip)")
                    zip_out = gr.File(label="Bundle (.zip)")
            run_btn.click(_run_generator,
                          [file_in, url_in, preset_in, aspect_in, provider_in,
                           clips_in, music_in, music_vol,
                           style_in, profile_in, subs_in,
                           cta_in, highlight_in, pacing_in,
                           clip_min_in, clip_max_in,
                           watermark_in, watermark_pos_in,
                           watermark_mode_in, logo_in],
                          [progress_out, ranking_out, files_out, job_dir_state])
            zip_btn.click(_zip_current, [job_dir_state], [zip_out])

        with gr.Tab("Batch"):
            batch_in = gr.Textbox(label="One file path or URL per line",
                                  lines=5)
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
            batch_btn.click(_batch_add, [batch_in], [batch_msg, batch_table])
            refresh_btn.click(_batch_rows, [], [batch_table])
            inbox_cb.change(_inbox_toggle, [inbox_cb], [batch_msg])
            batch_zip_btn.click(_zip_batch_all, [], [batch_zip_out])

        with gr.Tab("Edit clips"):
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
            job_dd.change(_job_clips, [job_dd], [clip_dd, edit_info])
            rerender_btn.click(_edit_rerender,
                               [job_dd, clip_dd, start_in, end_in, preset_edit],
                               [edit_video, edit_info])
            regen_btn.click(_edit_regen_meta, [job_dd, clip_dd], [edit_meta])

        with gr.Tab("YouTube upload"):
            upload_md = gr.Markdown(_upload_status())
            with gr.Row():
                auth_btn = gr.Button("Authorize YouTube")
                upload_job_dd = gr.Dropdown(_list_job_dirs(), label="Job")
                privacy_dd = gr.Dropdown(["private", "unlisted", "public"],
                                         value="private", label="Privacy")
                upload_btn = gr.Button("Upload kept clips", variant="primary")
            upload_result = gr.Markdown()
            auth_btn.click(_authorize, [], [upload_md])
            upload_btn.click(_upload_job, [upload_job_dd, privacy_dd],
                             [upload_result])

        with gr.Tab("History"):
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

        with gr.Tab("Settings"):
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
    build_app().queue().launch(server_name=host, server_port=port,
                               inbrowser=False, quiet=True,
                               theme=gr.themes.Soft())
