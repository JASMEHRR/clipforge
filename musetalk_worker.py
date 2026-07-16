"""MuseTalk lip-sync worker — runs ONLY under the isolated .venv-musetalk
interpreter (avatar.animation.lip_sync.python). MuseTalk pins its own
torch==2.0.1/cu118 build plus an mmcv/mmdet/mmpose (OpenMMLab) stack that
conflicts with LivePortrait's torch==2.3.0/cu121 in .venv-avatar-anim, so it
must never enter that venv or the main venv — see avatar_anim.MUSETALK_DIR.

Protocol (one job per process — MuseTalk has no batch API here):
  stdin  — a single JSON object:
             {"raw_video": str, "audio_path": str, "out_path": str,
              "musetalk_dir": str, "bbox_shift": int, "version": str,
              "ffmpeg_dir": str}
  stdout — exactly one JSON line:
             {"ok": true, "out_path": str, "duration_s": float}
           or {"ok": false, "error": str}
  stderr — human-readable progress/log lines only.
  exit   — 0 on ok, 1 otherwise.

No ClipForge imports: the isolated venv contains only MuseTalk and its
dependencies. `ffmpeg_dir` is resolved by the caller (avatar_anim.py, in the
main venv where ffutil.ffmpeg_bin() lives) and passed in as a plain string —
this worker never imports ffutil. Keep it that way.

Shells out to MuseTalk's own `scripts/inference.py` CLI (the documented,
stable entrypoint — there is no importable Python API) rather than importing
its internals, which move across commits.

ASSUMPTION (confirmed against scripts/inference.py source at plan time, but
re-verify if MuseTalk's pinned commit changes): no --device/--cpu flag
exists, only --gpu_id (a CUDA device index) and --use_float16. MuseTalk has
no documented/confirmed CPU inference path, so unlike liveportrait_worker.py
this worker does NOT attempt a CPU fallback — if CUDA isn't available it
fails loudly rather than silently trying an unverified code path.
"""
from __future__ import annotations

import json
import subprocess
import sys
import glob
import os
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _require_cuda() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            return
        raise RuntimeError("torch.cuda.is_available() is False")
    except Exception as e:
        raise RuntimeError(
            f"MuseTalk lip-sync requires CUDA and none is usable in "
            f".venv-musetalk ({e}) — MuseTalk's CLI has no confirmed CPU "
            "inference path, so there is no fallback within this worker; "
            "set avatar.animation.lip_sync.enabled: false or "
            "fallback_to_motion_only: true to skip lip-sync instead") from e


def _find_output(result_dir: Path, before: set[str]) -> Path:
    """MuseTalk's --result_dir layout/exact output filename isn't documented
    (its source builds the name from an internal temp_dir join we don't fully
    control even when --output_vid_name is passed) — find the newest .mp4
    anywhere under result_dir that wasn't there before the run, same
    defensive technique liveportrait_worker.py uses, but recursive since
    MuseTalk may nest output under a version/task subdirectory."""
    candidates = [p for p in glob.glob(str(result_dir / "**" / "*.mp4"),
                                       recursive=True) if p not in before]
    if not candidates:
        raise FileNotFoundError(
            f"MuseTalk produced no new .mp4 under {result_dir}")
    return Path(max(candidates, key=os.path.getmtime))


def main() -> int:
    try:
        req = json.load(sys.stdin)
        raw_video = Path(str(req["raw_video"]))
        audio_path = Path(str(req["audio_path"]))
        out_path = Path(str(req["out_path"]))
        mt_dir = Path(str(req["musetalk_dir"]))
        bbox_shift = int(req.get("bbox_shift", 0))
        version = str(req.get("version", "v15"))
        ffmpeg_dir = str(req["ffmpeg_dir"])
        if not raw_video.is_file():
            raise FileNotFoundError(f"raw video not found: {raw_video}")
        if not audio_path.is_file():
            raise FileNotFoundError(f"audio not found: {audio_path}")
        inference_py = mt_dir / "scripts" / "inference.py"
        if not (mt_dir / "scripts").is_dir():
            raise FileNotFoundError(
                f"MuseTalk scripts/ not found under {mt_dir} — run "
                "`python avatar.py setup-musetalk-venv`")

        _require_cuda()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        stage_dir = out_path.parent / f".musetalk_{out_path.stem}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        result_dir = stage_dir / "results"
        result_dir.mkdir(parents=True, exist_ok=True)
        before = set(glob.glob(str(result_dir / "**" / "*.mp4"),
                               recursive=True))

        # MuseTalk's inference config format (confirmed against
        # configs/inference/test.yaml): top-level keys are arbitrary task
        # IDs mapping to {video_path, audio_path, [bbox_shift]}.
        task_cfg = {
            "clipforge_lipsync": {
                "video_path": str(raw_video),
                "audio_path": str(audio_path),
                "bbox_shift": bbox_shift,
            }
        }
        cfg_path = stage_dir / "task.yaml"
        import yaml
        cfg_path.write_text(yaml.safe_dump(task_cfg), encoding="utf-8")

        version_dir = "musetalkV15" if version == "v15" else "musetalk"
        unet_name = "unet.pth" if version == "v15" else "pytorch_model.bin"
        # MuseTalk's own inference.sh runs `python3 -m scripts.inference`
        # (module mode, cwd = repo root) — NOT `python scripts/inference.py`
        # as a script file. Module mode is what puts the repo root (and so
        # the sibling `musetalk` package inference.py imports from) on
        # sys.path; script-file mode only adds scripts/ itself, which is
        # why `from musetalk.utils.blending import get_image` raised
        # ModuleNotFoundError under the old invocation (reproduced live).
        args = [
            sys.executable, "-m", "scripts.inference",
            "--inference_config", str(cfg_path),
            "--result_dir", str(result_dir),
            "--unet_model_path", str(mt_dir / "models" / version_dir / unet_name),
            "--unet_config", str(mt_dir / "models" / version_dir / "musetalk.json"),
            "--version", version,
            "--ffmpeg_path", ffmpeg_dir,
            "--use_float16",
        ]
        _log(f"running MuseTalk inference: {' '.join(args)}")
        # Same Windows console-codepage crash risk as LivePortrait's rich
        # progress output (see liveportrait_worker.py) — force UTF-8 stdio
        # defensively even though this hasn't been confirmed to reproduce.
        utf8_env = {**os.environ, "PYTHONUTF8": "1",
                   "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(args, cwd=str(mt_dir), capture_output=True,
                              text=True, encoding="utf-8", env=utf8_env)
        if proc.returncode != 0:
            raise RuntimeError(
                f"inference.py exited {proc.returncode}: "
                f"{(proc.stderr or '')[-1500:]}")

        produced = _find_output(result_dir, before)
        produced.replace(out_path)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
            capture_output=True, text=True)
        duration_s = float((probe.stdout or "").strip() or 0.0)

        print(json.dumps({"ok": True, "out_path": str(out_path),
                          "duration_s": round(duration_s, 3)}), flush=True)
        return 0
    except Exception as e:  # protocol: every failure is one JSON error line
        import traceback
        _log(traceback.format_exc())
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}),
              flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
