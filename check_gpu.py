"""Standalone GPU health check for ClipForge — plain-language verdict on
whether NVENC encoding and faster-whisper transcription will actually run on
the GPU, and the SPECIFIC reason when they won't.

Run:  .venv\\Scripts\\python.exe check_gpu.py

This exists because "GPU silently falls back to CPU" is impossible to diagnose
from a single log line. Here every check prints what it found and why.
"""
from __future__ import annotations

import subprocess

import ffutil


def _line(label: str, value: str) -> None:
    print(f"  {label:<22} {value}")


def _nvidia_smi() -> str | None:
    """Driver version string, or None if nvidia-smi is absent/failing."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main() -> int:
    print("ClipForge GPU check\n" + "=" * 40)

    print("\n[1] NVIDIA driver")
    gpu = _nvidia_smi()
    _line("nvidia-smi", gpu or "NOT FOUND (no NVIDIA GPU / driver)")

    print("\n[2] ffmpeg / NVENC encoding")
    fm = ffutil.ffmpeg_bin()
    _line("resolved ffmpeg", fm)
    try:
        _line("ffmpeg version", ffutil.ffmpeg_version())
    except Exception as e:  # noqa: BLE001
        _line("ffmpeg version", f"ERROR: {e}")
    enc_ok, enc_reason = ffutil._probe_nvenc()
    if enc_ok:
        _line("NVENC smoke encode", "SUCCESS (h264_nvenc)")
    else:
        _line("NVENC smoke encode", "FAILED")
        _line("  reason", enc_reason)

    print("\n[3] faster-whisper / CUDA transcription")
    ct_reason = ""
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        _line("ctranslate2", f"{ctranslate2.__version__}, cuda_devices={n}")
        trans_ok = n > 0
        if not trans_ok:
            ct_reason = "ctranslate2 reports 0 CUDA devices (CPU-only build?)"
    except Exception as e:  # noqa: BLE001
        trans_ok = False
        ct_reason = f"ctranslate2 unavailable: {e}"
        _line("ctranslate2", f"ERROR: {e}")
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            __import__(pkg)
            _line(pkg.replace(".", "-"), "installed")
        except Exception:  # noqa: BLE001
            _line(pkg.replace(".", "-"), "MISSING")

    print("\n" + "=" * 40)
    print("VERDICT")
    print(f"  GPU encode:    {'WORKING' if enc_ok else 'CPU fallback'}"
          + ("" if enc_ok else f" — {enc_reason}"))
    print(f"  GPU transcribe: {'WORKING' if trans_ok else 'CPU fallback'}"
          + ("" if trans_ok else f" — {ct_reason}"))
    if not enc_ok and gpu:
        print("\n  Note: a GPU is present but NVENC won't initialize. The usual\n"
              "  cause is an ffmpeg build whose NVENC SDK is newer than the\n"
              "  installed driver. Point CLIPFORGE_FFMPEG (or config ffmpeg.binary)\n"
              "  at a driver-compatible ffmpeg build. See README troubleshooting.")
    return 0 if (enc_ok and trans_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
