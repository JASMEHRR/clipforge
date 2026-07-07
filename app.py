"""Gradio UI (gradio 6.x): Create / Batch / Edit / Upload / History tabs.

All long work runs in background threads; progress streams through queues so
the UI thread never blocks. The YouTube OAuth flow is only ever started by an
explicit user click when client secrets are configured (never during builds)."""
from __future__ import annotations

import json
import queue
import threading
import traceback
from pathlib import Path

import gradio as gr

from config import ROOT, load_config
from logutil import get_logger

log = get_logger("app")


# --------------------------------------------------------------- create tab

def _virality_badge(vir: dict | None) -> str:
    """Green >=70 / yellow 40-69 / red <40 virality badge for the score table."""
    if not vir:
        return "-"
    score = vir.get("score", 0)
    dot = "🟢" if score >= 70 else "🟡" if score >= 40 else "🔴"
    return f"{dot} {int(score)} ({vir.get('verdict', '?')})"


def _run_generator(file_path, url, preset, aspect, provider, n_clips):
    from pipeline import run_job

    source = (url or "").strip() or file_path
    if not source:
        yield "Provide a video file or a URL.", "", []
        return
    target_count = int(n_clips) or None  # 0 = auto (keep-ratio rule)

    cfg = load_config()
    q: queue.Queue = queue.Queue()
    holder: dict = {}

    import time
    last = {"t": 0.0}

    def cb(stage, frac, msg):
        now = time.monotonic()
        if now - last["t"] < 1.0 and 0.01 < frac < 0.99:
            return  # throttle: at most ~1 update/sec
        last["t"] = now
        filled = int(frac * 24)
        bar = "█" * filled + "░" * (24 - filled)
        q.put(f"{bar} {frac * 100:3.0f}%  {stage}: {msg}")

    def work():
        try:
            holder["job"] = run_job(source, cfg, provider=provider or None,
                                    preset=preset or None,
                                    aspect=aspect or "9:16",
                                    target_count=target_count, progress_cb=cb)
        except Exception as e:  # noqa: BLE001 — UI must show, not crash
            holder["error"] = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    lines: list[str] = [f"Job started: {source}"]
    yield "\n".join(lines), "", []

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
        yield "\n".join(lines[-25:]), "", []

    if "error" in holder:
        lines.append(f"FAILED: {holder['error']}")
        yield "\n".join(lines[-25:]), "", []
        return

    job = holder["job"]
    kept = [c for c in job["clips"] if c.get("kept")]
    kept.sort(key=lambda c: -c.get("virality", {}).get("score", 0))
    md = ["| # | virality | quality | length | title | preset |",
          "|---|---|---|---|---|---|"]
    for rank, c in enumerate(kept, 1):
        md.append(f"| {rank} | {_virality_badge(c.get('virality'))} | "
                  f"{c['weighted_score']:.2f} | "
                  f"{c['duration']:.0f}s | {c['metadata']['title']} | "
                  f"{c['preset']} |")
    files = [c["path"] for c in kept if Path(c["path"]).exists()]
    files += [c["srt"] for c in kept if Path(c.get("srt", "")).exists()]
    lines.append(f"Done: {len(kept)} clips kept of {len(job['clips'])} "
                 f"rendered → {job['job_dir']}")
    yield "\n".join(lines[-25:]), "\n".join(md), files


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
    from rerender import rerender_clip
    if not (job_name and clip_choice):
        return None, "select a job and a clip"
    try:
        idx = int(clip_choice.split("|")[0].strip())
        job_dir = ROOT / load_config()["paths"]["output_dir"] / job_name
        clip = rerender_clip(job_dir, idx, float(start), float(end),
                             preset or None)
        return clip["path"], (f"re-rendered clip {idx:02d}: "
                              f"{clip['start']:.2f}–{clip['end']:.2f}s "
                              f"(snapped to sentence boundaries)")
    except Exception as e:  # noqa: BLE001
        return None, f"re-render failed: {e}"


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
    from history import list_jobs
    rows = [[j["created"], j["job_id"], j["source"][-50:], j["status"],
             f"{j['kept']}/{j['clip_count']}"] for j in list_jobs()]
    return rows or [["-", "-", "no jobs yet", "-", "-"]]


def _history_open(job_id):
    from history import get_job
    if not job_id:
        return [], "enter a job id from the table"
    job = get_job(job_id.strip())
    if job is None:
        return [], f"job '{job_id}' not found"
    files = [c["path"] for c in job["clips"]
             if c.get("kept") and Path(c["path"]).exists()]
    return files, f"{len(files)} downloadable clips from {job['job_dir']}"


# ------------------------------------------------------------------- layout

def build_app() -> gr.Blocks:
    cfg = load_config()
    presets = list(cfg["captions"]["presets"].keys())
    with gr.Blocks(title="ClipForge") as demo:
        gr.Markdown("# ClipForge — local video repurposing\n"
                    "Long video in → ranked vertical clips with animated "
                    "captions out. No API key required (mock provider); add "
                    "GEMINI_API_KEY in .env for LLM-powered selection.")
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
                    provider_in = gr.Dropdown(
                        ["", "mock", "gemini", "groq", "ollama"], value="",
                        label="LLM provider override")
                    run_btn = gr.Button("Create clips", variant="primary")
                with gr.Column(scale=2):
                    progress_out = gr.Textbox(label="Progress", lines=10)
                    ranking_out = gr.Markdown()
                    files_out = gr.Files(label="Download clips + subtitles")
            run_btn.click(_run_generator,
                          [file_in, url_in, preset_in, aspect_in, provider_in,
                           clips_in],
                          [progress_out, ranking_out, files_out])

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
            batch_btn.click(_batch_add, [batch_in], [batch_msg, batch_table])
            refresh_btn.click(_batch_rows, [], [batch_table])
            inbox_cb.change(_inbox_toggle, [inbox_cb], [batch_msg])

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
                headers=["created", "job_id", "source", "status", "kept"],
                value=_history_rows(), interactive=False)
            hist_msg = gr.Markdown()
            hist_files = gr.Files(label="Clips")
            hist_refresh.click(_history_rows, [], [hist_table])
            hist_open.click(_history_open, [hist_id], [hist_files, hist_msg])
    return demo


if __name__ == "__main__":
    import os
    port = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    host = os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1")  # 0.0.0.0 in Docker
    build_app().queue().launch(server_name=host, server_port=port,
                               inbrowser=False, quiet=True)
