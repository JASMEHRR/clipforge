"""Gate 2 verification (keyless):
1. batch of 2 sample videos completes with per-job statuses (failure isolated)
2. all 4 caption presets render correctly on one clip each
3. editing a clip's bounds re-renders ONLY that clip (others untouched,
   transcription not re-run)
4. thumbnails exist (3 per kept clip)
5. upload path shows correct setup guidance with creds absent AND the
   mocked-API unit tests pass

Usage: python scripts/gate2.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("YOUTUBE_CLIENT_SECRETS", None)

from config import ROOT, load_config  # noqa: E402

failures: list[str] = []
cfg = load_config()

# ---- 1. batch of two sample videos ------------------------------------
from batch import JobQueue  # noqa: E402

q = JobQueue(cfg)
q.add(str(ROOT / "samples" / "sample.mp4"))
q.add(str(ROOT / "samples" / "synthetic_test.mp4"))
q.add("Z:/does/not/exist.mp4")   # failure isolation: must not kill the queue

print("batch: waiting for 3 queued jobs (2 real + 1 doomed)…")
deadline = time.time() + 60 * 40
while time.time() < deadline:
    rows = q.status_rows()
    if all(r[2] in ("done", "failed") for r in rows):
        break
    time.sleep(10)
rows = q.status_rows()
for r in rows:
    print(f"  job {r[0]}: {r[2]} — {r[3]} ({r[1]})")
statuses = [r[2] for r in rows]
if statuses[:2] != ["done", "done"]:
    failures.append(f"batch: expected first two jobs done, got {statuses}")
if statuses[2] != "failed":
    failures.append("batch: doomed job did not fail cleanly")
if len([r for r in rows if r[2] == "done"]) < 2:
    failures.append("batch: fewer than 2 completed jobs")

done_dirs = [i["job_dir"] for i in q.items if i["status"] == "done"]

# ---- 2. all 4 caption presets on one clip each -------------------------
import captions as cap  # noqa: E402

job1 = Path(done_dirs[0])
src = job1 / "clip_00" / "reframed.mp4"
words_all = json.loads((job1 / ".done_transcribe.json").read_text(
    encoding="utf-8"))["words"]
job = json.loads((job1 / "job.json").read_text(encoding="utf-8"))
c0 = job["clips"][0]
words = [{"word": w["word"], "start": round(w["start"] - c0["start"], 3),
          "end": round(w["end"] - c0["start"], 3)}
         for w in words_all
         if w["start"] >= c0["start"] - 0.05 and w["end"] <= c0["end"] + 0.05]
preset_dir = job1 / "preset_check"
preset_dir.mkdir(exist_ok=True)
for preset in cfg["captions"]["presets"]:
    out = preset_dir / f"{preset}.mp4"
    try:
        cap.caption_clip(src, words, out, cfg, preset_name=preset)
        if not out.exists() or out.stat().st_size < 50_000:
            failures.append(f"preset {preset}: output missing/too small")
        else:
            print(f"  preset {preset}: rendered OK")
        if not out.with_suffix(".srt").exists():
            failures.append(f"preset {preset}: .srt missing")
    except Exception as e:  # noqa: BLE001
        failures.append(f"preset {preset}: {e}")

# ---- 3. targeted re-render ---------------------------------------------
from rerender import rerender_clip  # noqa: E402

other_mtimes = {p: p.stat().st_mtime
                for p in job1.glob("clip_*/final.mp4")
                if "clip_00" not in str(p)}
transcribe_marker = (job1 / ".done_transcribe.json").stat().st_mtime
new_start = c0["start"] + 5
try:
    clip = rerender_clip(job1, 0, new_start, c0["end"] + 5, cfg=cfg)
    if abs(clip["start"] - c0["start"]) < 0.01 and abs(clip["end"] - c0["end"]) < 0.01:
        failures.append("re-render: bounds did not change")
    print(f"  re-rendered clip 00 to {clip['start']:.2f}-{clip['end']:.2f} "
          "(sentence-snapped)")
except Exception as e:  # noqa: BLE001
    failures.append(f"re-render failed: {e}")
for p, m in other_mtimes.items():
    if p.stat().st_mtime != m:
        failures.append(f"re-render touched other clip: {p}")
if (job1 / ".done_transcribe.json").stat().st_mtime != transcribe_marker:
    failures.append("re-render re-ran transcription")

# ---- 4. thumbnails -------------------------------------------------------
for d in done_dirs:
    jd = json.loads((Path(d) / "job.json").read_text(encoding="utf-8"))
    for c in jd["clips"]:
        if c.get("kept"):
            thumbs = [t for t in c.get("thumbnails", []) if Path(t).exists()]
            if len(thumbs) != 3:
                failures.append(f"{Path(d).name} clip {c['index']:02d}: "
                                f"{len(thumbs)} thumbnails (want 3)")

# ---- 5. upload guidance + mocked tests ----------------------------------
from app import _upload_status  # noqa: E402

status = _upload_status()
if "console.cloud.google.com" not in status or "not configured" not in status:
    failures.append("upload tab does not show setup guidance without creds")
else:
    print("  upload guidance shown correctly with creds absent")

r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_upload.py",
                    "-q"], capture_output=True, text=True, cwd=ROOT)
if r.returncode != 0:
    failures.append(f"mocked upload tests failed:\n{r.stdout[-800:]}")
else:
    print("  mocked upload unit tests: green")

if failures:
    print("GATE 2 FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("GATE 2 PASSED")
