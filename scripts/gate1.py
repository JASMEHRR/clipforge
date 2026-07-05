"""Gate 1 verification (run keyless, after `pipeline.py --sample --provider mock --debug`):

- job.json: status done, >=3 kept clips
- every kept clip: final.mp4 exists, 1080x1920, duration 30-60s, valid
  ClipMetadata, starts/ends on sentence boundaries, smoothness_ok
- DEBUG artifacts present (transcript, prompts, candidates, reframe frames)
- Gradio smoke: background launch, HTTP poll, kill

Usage: python scripts/gate1.py [job_dir]
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import ROOT, load_config  # noqa: E402
from ffutil import probe  # noqa: E402
from schemas import validate  # noqa: E402

failures: list[str] = []
cfg = load_config()


def latest_job_dir() -> Path:
    out = ROOT / cfg["paths"]["output_dir"]
    jobs = sorted([d for d in out.iterdir()
                   if d.is_dir() and (d / "job.json").exists()])
    if not jobs:
        print("GATE 1 FAILED: no job dirs found")
        sys.exit(1)
    return jobs[-1]


job_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_job_dir()
print(f"checking job: {job_dir}")
job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
validate(job, "job_record")

if job["status"] != "done":
    failures.append(f"job status = {job['status']}")

kept = [c for c in job["clips"] if c.get("kept")]
if len(kept) < 3:
    failures.append(f"only {len(kept)} kept clips (need >=3)")

transcript = None
tdir = job_dir / "debug" / "transcript.json"
if tdir.exists():
    transcript = json.loads(tdir.read_text(encoding="utf-8"))
else:
    caches = sorted((ROOT / "cache/transcripts").glob("*.json"))
    if caches:
        transcript = json.loads(caches[-1].read_text(encoding="utf-8"))

sent_starts = {round(s["start"], 3) for s in transcript["sentences"]} if transcript else set()
sent_ends = {round(s["end"], 3) for s in transcript["sentences"]} if transcript else set()
mechanical = transcript is not None and not transcript["sentences"]

for c in kept:
    tag = f"clip {c['index']:02d}"
    p = Path(c["path"])
    if not p.exists():
        failures.append(f"{tag}: final.mp4 missing")
        continue
    info = probe(p)
    if c.get("aspect", "9:16") == "9:16" and (info["width"], info["height"]) != (1080, 1920):
        failures.append(f"{tag}: {info['width']}x{info['height']} not 1080x1920")
    dur = c["end"] - c["start"]
    if not (cfg["clips"]["min_seconds"] - 0.01 <= dur <= cfg["clips"]["max_seconds"] + 0.01):
        failures.append(f"{tag}: duration {dur:.1f}s out of bounds")
    try:
        validate(c["metadata"], "clip_metadata")
    except Exception as e:  # noqa: BLE001
        failures.append(f"{tag}: metadata invalid: {e}")
    if not mechanical and sent_starts:
        if round(c["start"], 3) not in sent_starts:
            failures.append(f"{tag}: start {c['start']} not on a sentence boundary")
        if round(c["end"], 3) not in sent_ends:
            failures.append(f"{tag}: end {c['end']} not on a sentence boundary")
    rf = c.get("reframe", {})
    if not rf.get("passthrough") and not rf.get("smoothness_ok"):
        failures.append(f"{tag}: smoothness thresholds failed: {rf.get('smoothness')}")
    if not Path(c["srt"]).exists():
        failures.append(f"{tag}: .srt missing")

if job["settings"].get("debug"):
    dbg = job_dir / "debug"
    for artifact in ("transcript.json", "highlight_prompt.txt", "candidates.json"):
        if not (dbg / artifact).exists():
            failures.append(f"debug artifact missing: {artifact}")
    frames = list(dbg.glob("reframe_frames/*/*.jpg"))
    if len(frames) < 10:
        failures.append(f"debug reframe frames: {len(frames)} (<10)")
    else:
        print(f"  debug reframe frames present: {len(frames)}")

# ---- Gradio smoke: background launch, poll, kill (rule 4)
print("gradio smoke: launching…")
env = dict(os.environ, GRADIO_SERVER_PORT="7861")
env.pop("GEMINI_API_KEY", None)
proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], env=env,
                        stdout=open(job_dir / "gradio_smoke.log", "w"),
                        stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
import requests  # noqa: E402

ok = False
for _ in range(60):
    time.sleep(2)
    try:
        r = requests.get("http://127.0.0.1:7861/", timeout=3)
        if r.status_code == 200 and "gradio" in r.text.lower():
            ok = True
            break
    except requests.RequestException:
        pass
if not ok:
    failures.append("gradio did not answer on http://127.0.0.1:7861 within 120s")
else:
    print("  gradio answered 200 OK")
proc.kill()

if failures:
    print("GATE 1 FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"GATE 1 PASSED — {len(kept)} kept clips, all checks green")
