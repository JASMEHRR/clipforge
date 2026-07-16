"""LivePortrait animation worker — runs ONLY under the isolated
.venv-avatar-anim interpreter (avatar.animation.python). LivePortrait pins its
own torch/torchvision build and has no pip package (git clone only), so it
must never enter the main venv — see avatar_anim.LIVEPORTRAIT_DIR.

Protocol (one job per process — LivePortrait has no batch API):
  stdin  — a single JSON object:
             {"source_image": str, "driving_video": str, "out_path": str,
              "liveportrait_dir": str, "device": "auto"|"cpu"}
  stdout — exactly one JSON line:
             {"ok": true, "out_path": str, "duration_s": float}
           or {"ok": false, "error": str}
  stderr — human-readable progress/log lines only.
  exit   — 0 on ok, 1 otherwise.

No ClipForge imports: the isolated venv contains only LivePortrait and its
dependencies. Keep it that way.

Shells out to LivePortrait's own `inference.py` CLI (the documented, stable
entrypoint) rather than importing its internals, which move across commits.
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


def _pick_device(requested: str) -> str:
    if requested == "cpu":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        _log("CUDA not available — using CPU (expect minutes per segment)")
    except Exception as e:  # torch import/probe must never kill the job
        _log(f"torch CUDA probe failed ({e}) — using CPU")
    return "cpu"


def _find_output(out_dir: Path, before: set[str]) -> Path:
    """LivePortrait writes its result into --output-dir with a name derived
    from the source/driving filenames (format varies by version) — find the
    newest .mp4 that wasn't there before the run rather than guessing the
    exact name."""
    candidates = [p for p in glob.glob(str(out_dir / "*.mp4")) if p not in before]
    if not candidates:
        raise FileNotFoundError(
            f"LivePortrait produced no new .mp4 in {out_dir}")
    return Path(max(candidates, key=os.path.getmtime))


def main() -> int:
    try:
        req = json.load(sys.stdin)
        source = Path(str(req["source_image"]))
        driving = Path(str(req["driving_video"]))
        out_path = Path(str(req["out_path"]))
        lp_dir = Path(str(req["liveportrait_dir"]))
        if not source.is_file():
            raise FileNotFoundError(f"source image not found: {source}")
        if not driving.is_file():
            raise FileNotFoundError(f"driving video not found: {driving}")
        inference_py = lp_dir / "inference.py"
        if not inference_py.is_file():
            raise FileNotFoundError(
                f"LivePortrait inference.py not found under {lp_dir} — "
                "run `python avatar.py setup-anim-venv`")

        device = _pick_device(str(req.get("device", "auto")))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stage_dir = out_path.parent / f".liveportrait_{out_path.stem}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        before = set(glob.glob(str(stage_dir / "*.mp4")))

        args = [sys.executable, str(inference_py),
                "--source", str(source), "--driving", str(driving),
                "--output-dir", str(stage_dir), "--no-flag-pasteback"]
        if device == "cpu":
            args += ["--flag-force-cpu"]
        _log(f"running LivePortrait inference on {device}: "
             f"{' '.join(args)}")
        # inference.py prints emoji via rich progress bars — subprocess.run
        # on Windows defaults stdio to the console codepage (cp1252), which
        # can't encode them and crashes the whole process after computation
        # already finished. Force UTF-8 stdio (see avatar_anim._UTF8_ENV).
        utf8_env = {**os.environ, "PYTHONUTF8": "1",
                   "PYTHONIOENCODING": "utf-8"}
        # onnxruntime-gpu 1.18's CUDAExecutionProvider is built against CUDA
        # 11.8 + cuDNN 8 (confirmed via its DLL imports: cublas64_11,
        # cudart64_110, cudnn64_8, cufft64_10) -- NOT the CUDA 12 runtime
        # torch bundles in torch/lib, so torch's DLLs can't satisfy it even
        # though torch.cuda itself works fine. The matching versions come
        # from the pip-installed nvidia-cuda-runtime-cu11/nvidia-cublas-cu11/
        # nvidia-cudnn-cu11/nvidia-cufft-cu11 wheels (this same venv); their
        # DLL dirs aren't on PATH by default, so prepend them for this child
        # process only.
        site_packages = Path(sys.executable).parent.parent / "Lib" / "site-packages" / "nvidia"
        dll_dirs = [str(site_packages / "cublas" / "bin"),
                   str(site_packages / "cuda_runtime" / "bin"),
                   str(site_packages / "cudnn" / "bin"),
                   str(site_packages / "cufft" / "bin")]
        utf8_env["PATH"] = os.pathsep.join(dll_dirs) + os.pathsep + utf8_env.get("PATH", "")
        proc = subprocess.run(args, cwd=str(lp_dir), capture_output=True,
                              text=True, encoding="utf-8", env=utf8_env)
        if proc.returncode != 0:
            raise RuntimeError(
                f"inference.py exited {proc.returncode}: "
                f"{(proc.stderr or '')[-1500:]}")

        produced = _find_output(stage_dir, before)
        produced.replace(out_path)

        import subprocess as sp
        probe = sp.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of",
                        "default=noprint_wrappers=1:nokey=1", str(out_path)],
                       capture_output=True, text=True)
        duration_s = float(probe.stdout.strip() or 0.0)

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
