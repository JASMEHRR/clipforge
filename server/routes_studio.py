"""Studio: start the from-scratch makers (money explainer, trending reels, a
reel from a pasted post) from the UI and watch their progress.

Each run is the same CLI the nightly script uses (explain.py / scout.py),
started as a child process with its output in output/studio.log. One run at
a time: they all need the GPU. Nothing here publishes anything - the clips
land in their workspace's approvals.
"""
from __future__ import annotations

import subprocess
import sys
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import ROOT, base_output_dir

router = APIRouter()
_state: dict = {"proc": None, "kind": "", "started": 0.0}
KINDS = {"money": "Money video + reels", "trending": "Trending reels",
         "story": "Reel from a post"}


class StudioRun(BaseModel):
    kind: str
    topic: str = Field("", max_length=200)          # money: optional topic
    count: int = Field(2, ge=1, le=5)                # trending: how many reels
    text: str = Field("", max_length=1200)           # story: the post
    author: str = Field("", max_length=80)
    source: str = Field("", max_length=80)
    context: str = Field("", max_length=4000)        # story: background facts


def _log_path():
    return base_output_dir() / "studio.log"


def command(req: StudioRun) -> list[str]:
    """Pure. The CLI for a run request (raises ValueError on a bad one)."""
    py = sys.executable
    if req.kind == "money":
        return [py, "explain.py", "--count", "1"] + (["--topic", req.topic.strip()]
                                                     if req.topic.strip() else [])
    if req.kind == "trending":
        return [py, "scout.py", "--make", str(req.count)]
    if req.kind == "story":
        if not req.text.strip():
            raise ValueError("Paste the post's text first.")
        return [py, "explain.py", "--story", req.text.strip(),
                "--author", req.author.strip() or "Someone",
                "--source", req.source.strip(), "--context", req.context.strip()]
    raise ValueError("Unknown kind of video.")


def _running() -> bool:
    p = _state["proc"]
    return p is not None and p.poll() is None


def readable(lines: list[str]) -> list[str]:
    """Pure. The log lines worth showing: stage progress and results, without
    ffmpeg command echoes or long tracebacks."""
    keep = []
    for ln in lines:
        s = ln.strip()
        if not s or "ffmpeg error -i" in s or "ffmpeg rawvideo" in s or s.startswith("File "):
            continue
        keep.append(s[:220])
    return keep[-14:]


@router.post("/api/studio/run")
def run(req: StudioRun):
    if _running():
        raise HTTPException(409, f"{KINDS.get(_state['kind'], 'A video')} is still being made. "
                                 "Wait for it to finish first.")
    try:
        cmd = command(req)
    except ValueError as e:
        raise HTTPException(422, str(e))
    log = _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"==== {KINDS[req.kind]} started {time.strftime('%d %b %I:%M %p')} ====\n")
    out = open(log, "a", encoding="utf-8")
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    _state.update(proc=subprocess.Popen(cmd, cwd=str(ROOT), stdout=out,
                                        stderr=subprocess.STDOUT, env=env),
                  kind=req.kind, started=time.time())
    out.close()                    # the child keeps its own handle
    return {"started": req.kind}


@router.get("/api/studio/status")
def status():
    log = _log_path()
    lines: list[str] = []
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    p = _state["proc"]
    return {"running": _running(), "kind": _state["kind"],
            "label": KINDS.get(_state["kind"], ""),
            "minutes": round((time.time() - _state["started"]) / 60, 1) if _running() else 0,
            "exit_code": None if p is None or _running() else p.returncode,
            "lines": readable(lines)}
