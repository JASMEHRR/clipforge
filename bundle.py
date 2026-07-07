"""Bulk download: zip a job's kept clips (final.mp4 + .srt + metadata.json)
into a single archive for one-click download."""
from __future__ import annotations

import datetime as dt
import json
import zipfile
from pathlib import Path

from config import ROOT, load_config
from errors import ClipForgeError
from logutil import get_logger

log = get_logger("bundle")


def _load_job(job_dir: Path) -> dict:
    p = job_dir / "job.json"
    if not p.exists():
        raise ClipForgeError(f"no job.json in {job_dir}")
    return json.loads(p.read_text(encoding="utf-8"))


def _add_clip(z: zipfile.ZipFile, clip: dict, prefix: str = "") -> int:
    """Add a clip's final.mp4, .srt and metadata.json under prefix/clip_NN/.
    Returns the number of files added."""
    final = Path(clip.get("path", ""))
    if not final.exists():
        return 0
    clip_dir = final.parent
    arc = f"{prefix}{clip_dir.name}"
    added = 0
    for src in (final, Path(clip.get("srt", "")), clip_dir / "metadata.json"):
        if src and src.exists():
            z.write(src, f"{arc}/{src.name}")
            added += 1
    return added


def zip_job(job_dir: str | Path, kept_only: bool = True) -> Path:
    """Zip a single job's clips into <job_dir>/clips_bundle.zip."""
    job_dir = Path(job_dir)
    job = _load_job(job_dir)
    out = job_dir / "clips_bundle.zip"
    clips = [c for c in job["clips"] if c.get("kept") or not kept_only]
    if not clips:
        raise ClipForgeError(f"no clips to bundle in {job_dir}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        total = sum(_add_clip(z, c) for c in clips)
    if total == 0:
        out.unlink(missing_ok=True)
        raise ClipForgeError(f"no clip files found on disk in {job_dir}")
    log.info("bundled %d files from %d clips -> %s", total, len(clips), out.name)
    return out


def zip_jobs(job_dirs: list[str | Path], cfg: dict | None = None,
             kept_only: bool = True) -> Path:
    """Zip several jobs' clips into one archive (namespaced per job) under
    output/_bundle_<ts>.zip."""
    cfg = cfg or load_config()
    dirs = [Path(d) for d in job_dirs]
    if not dirs:
        raise ClipForgeError("no jobs to bundle")
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / cfg["paths"]["output_dir"] / f"_bundle_{ts}.zip"
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for d in dirs:
            try:
                job = _load_job(d)
            except ClipForgeError:
                continue
            for c in job["clips"]:
                if c.get("kept") or not kept_only:
                    total += _add_clip(z, c, prefix=f"{d.name}/")
    if total == 0:
        out.unlink(missing_ok=True)
        raise ClipForgeError("no clip files found across the given jobs")
    log.info("bundled %d files from %d jobs -> %s", total, len(dirs), out.name)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="smoke: zip a job's clips")
    ap.add_argument("job_dir")
    a = ap.parse_args()
    print(zip_job(a.job_dir))
