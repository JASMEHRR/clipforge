"""Hallo2 talking-head worker — runs ONLY under the isolated .venv-hallo2
interpreter (avatar.animation.hallo2.python). Hallo2 (fudan-generative-vision)
pins Python 3.10 + torch==2.2.2/cu118 and a diffusers/mmpose stack, so it must
never enter the main venv — see avatar_anim.HALLO2_DIR.

Hallo2 is audio-driven: it animates a single source portrait image directly
from a driving audio wav (no driving video, no separate lip-sync stage), which
is why it REPLACES the old LivePortrait(+MuseTalk) two-stage engine.

Protocol (one job per process):
  stdin  — a single JSON object:
             {"source_image": str, "driving_audio": str, "out_path": str,
              "hallo2_dir": str, "pretrained_dir": str, "config": str,
              "pose_weight": float, "face_weight": float, "lip_weight": float,
              "face_expand_ratio": float}
  stdout — exactly one JSON line:
             {"ok": true, "out_path": str, "duration_s": float}
           or {"ok": false, "error": str}
  stderr — human-readable progress/log lines only.
  exit   — 0 on ok, 1 otherwise.

No ClipForge imports: the isolated venv contains only Hallo2 and its deps.

Shells out to Hallo2's own `scripts/inference_long.py` CLI (the documented,
stable entrypoint) rather than importing its internals. Inference is driven by
a YAML config; source_image/driving_audio/save_path come from the config, and
the documented CLI overrides (--source_image/--driving_audio/--pose_weight/
--face_weight/--lip_weight/--face_expand_ratio) pin the rest. This worker copies
the repo's base long.yaml, points save_path at a private staging dir, and finds
the newest .mp4 it produces (Hallo2's exact output filename isn't documented —
same defensive technique musetalk_worker.py uses).

NOTE: untested on the author's machine (no GPU that fits Hallo2 — it targets
~A100-class cards). Built to Hallo2's documented interface; validate on a
capable GPU box and adjust the arg names here if the pinned commit differs.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _require_cuda() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            return
        raise RuntimeError("torch.cuda.is_available() is False")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Hallo2 requires CUDA and none is usable in .venv-hallo2 ({e}) — "
            "Hallo2 has no CPU inference path. Run it on a CUDA GPU with enough "
            "VRAM (it targets ~A100-class cards).") from e


def _find_output(save_dir: Path, before: set[str]) -> Path:
    """Newest .mp4 anywhere under save_dir that wasn't there before the run."""
    candidates = [p for p in glob.glob(str(save_dir / "**" / "*.mp4"),
                                       recursive=True) if p not in before]
    if not candidates:
        raise FileNotFoundError(f"Hallo2 produced no new .mp4 under {save_dir}")
    return Path(max(candidates, key=os.path.getmtime))


def main() -> int:
    try:
        req = json.load(sys.stdin)
        source_image = Path(str(req["source_image"]))
        driving_audio = Path(str(req["driving_audio"]))
        out_path = Path(str(req["out_path"]))
        hallo2_dir = Path(str(req["hallo2_dir"]))
        base_config = Path(str(req.get("config")
                               or hallo2_dir / "configs" / "inference" / "long.yaml"))
        if not source_image.is_file():
            raise FileNotFoundError(f"source image not found: {source_image}")
        if not driving_audio.is_file():
            raise FileNotFoundError(f"driving audio not found: {driving_audio}")
        if not (hallo2_dir / "scripts" / "inference_long.py").is_file():
            raise FileNotFoundError(
                f"Hallo2 scripts/inference_long.py not found under {hallo2_dir} — "
                "run `python avatar.py setup-hallo2`")
        if not base_config.is_file():
            raise FileNotFoundError(f"Hallo2 config not found: {base_config}")

        _require_cuda()

        import yaml
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stage_dir = out_path.parent / f".hallo2_{out_path.stem}"
        save_dir = stage_dir / "results"
        save_dir.mkdir(parents=True, exist_ok=True)
        before = set(glob.glob(str(save_dir / "**" / "*.mp4"), recursive=True))

        # copy the repo's base long.yaml and point the inputs + save_path at us
        cfg = yaml.safe_load(base_config.read_text(encoding="utf-8")) or {}
        cfg["source_image"] = str(source_image)
        cfg["driving_audio"] = str(driving_audio)
        cfg["save_path"] = str(save_dir)
        cfg_path = stage_dir / "task.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        # Hallo2's inference is a module run from the repo root (so its own
        # packages import); CLI overrides pin the documented weights.
        args = [
            sys.executable, "-m", "scripts.inference_long",
            "--config", str(cfg_path),
            "--source_image", str(source_image),
            "--driving_audio", str(driving_audio),
            "--pose_weight", str(req.get("pose_weight", 1.0)),
            "--face_weight", str(req.get("face_weight", 1.0)),
            "--lip_weight", str(req.get("lip_weight", 1.0)),
            "--face_expand_ratio", str(req.get("face_expand_ratio", 1.2)),
        ]
        _log(f"running Hallo2 inference: {' '.join(args)}")
        utf8_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(args, cwd=str(hallo2_dir), capture_output=True,
                              text=True, encoding="utf-8", env=utf8_env)
        if proc.returncode != 0:
            raise RuntimeError(
                f"inference_long.py exited {proc.returncode}: "
                f"{(proc.stderr or '')[-1500:]}")

        produced = _find_output(save_dir, before)
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
