"""Generate a synthetic sample video (rule-6 fallback when all sample mirrors fail).

Produces ~3 minutes of 1280x720 video: distinct colored scenes with drawtext
titles (gives PySceneDetect real shot boundaries) and narration audio.
Audio: Windows TTS (System.Speech via PowerShell) when available so the sample
is genuinely transcribable; otherwise a sine sweep (pipeline then runs
"mechanically" — see PROGRESS.md Known Issues).

Usage: python scripts/make_synthetic_sample.py <out.mp4>
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from logutil import get_logger  # noqa: E402

log = get_logger("synthsample")

NARRATION = (
    "Welcome to this short talk about building things that last. "
    "Here is the surprising truth. Most projects fail not because of bad code, "
    "but because of unclear goals. Think about the last tool you abandoned. "
    "It probably worked fine. It just never solved a problem you actually had. "
    "So before you write a single line, write down the one sentence that "
    "explains who needs this and why. That sentence is your compass. "
    "When a feature request arrives, test it against the sentence. "
    "If it does not serve the sentence, it does not ship. "
    "This sounds simple, and it is. Simple rules survive contact with reality. "
    "Complicated rules get ignored on the first busy week. "
    "Now let us talk about momentum. Small wins compound. "
    "Ship something tiny every day and the project stays alive in your mind. "
    "Skip a week and the code turns into a stranger's house. "
    "Finally, remember that finished beats perfect. "
    "A shipped tool with rough edges helps someone today. "
    "A perfect design document helps no one. Thank you for listening."
)

SCENES = [
    ("BUILD THINGS THAT LAST", "0x1a1a2e"),
    ("THE ONE SENTENCE RULE", "0x16324f"),
    ("SIMPLE RULES SURVIVE", "0x2e1a47"),
    ("MOMENTUM COMPOUNDS", "0x1f3d2b"),
    ("FINISHED BEATS PERFECT", "0x4f2d16"),
]


def _tts_windows(text: str, wav_path: Path) -> bool:
    if not shutil.which("powershell"):
        return False
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 0; "
        f"$s.SetOutputToWaveFile('{wav_path}'); "
        "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=text.encode("utf-8"), capture_output=True, timeout=300)
        return r.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 10000
    except Exception as e:
        log.warning("TTS failed: %s", e)
        return False


def _audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
    return float(out.stdout.strip())


def make_sample(out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="clipforge_synth_"))
    wav = tmp / "narration.wav"

    if _tts_windows(NARRATION, wav):
        log.info("narration via Windows TTS (%s)", wav)
        duration = _audio_duration(wav)
        audio_in = ["-i", str(wav)]
    else:
        log.warning("TTS unavailable — sine-sweep audio (mechanical run only)")
        duration = 180.0
        audio_in = ["-f", "lavfi", "-i",
                    f"sine=frequency=440:beep_factor=4:duration={duration}"]

    per_scene = duration / len(SCENES)
    font = Path(__file__).resolve().parents[1] / "assets/fonts/Montserrat-Bold.ttf"
    fontfile = str(font).replace("\\", "/").replace(":", r"\:")

    # One color+drawtext source per scene, concatenated: real cut boundaries.
    inputs, filters, concat = [], [], ""
    for i, (title, color) in enumerate(SCENES):
        inputs += ["-f", "lavfi", "-t", f"{per_scene:.3f}",
                   "-i", f"color=c={color}:s=1280x720:r=30"]
        filters.append(
            f"[{i}:v]drawtext=fontfile='{fontfile}':text='{title}':"
            "fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2[v{i}]"
            .replace("{i}", str(i)))
        concat += f"[v{i}]"
    fc = ";".join(filters) + f";{concat}concat=n={len(SCENES)}:v=1:a=0[vout]"

    cmd = (["ffmpeg", "-y", "-v", "error"] + inputs + audio_in +
           ["-filter_complex", fc, "-map", "[vout]", "-map", f"{len(SCENES)}:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(out_path)])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"synthetic sample ffmpeg failed: {r.stderr[-800:]}")
    log.info("synthetic sample written: %s (%.0fs)", out_path, duration)
    return out_path


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/synthetic.mp4")
    make_sample(dest)
    print(dest)
