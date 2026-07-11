"""Background music is actually audible in the mixed clip.

Regression guard for the reported bug where selected music was inaudible:
add_music's base attenuation stacked with an over-aggressive sidechain duck
(threshold=0.02:ratio=8:release=300) left the bed around -40 dB. These are
real-ffmpeg integration tests (skipped if ffmpeg is unavailable) — a mock
can't measure loudness.
"""
import re
import subprocess

import pytest

import ffutil
import music
from config import load_config


def _ffmpeg_ok() -> bool:
    try:
        subprocess.run([ffutil.ffmpeg_bin(), "-version"],
                       capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_ok(),
                                reason="ffmpeg not available")

_BIN = None


def _bin() -> str:
    global _BIN
    if _BIN is None:
        _BIN = ffutil.ffmpeg_bin()
    return _BIN


def _run(args: list[str]) -> str:
    """Run ffmpeg at info verbosity and return stderr (where volumedetect and
    astats print their measurements)."""
    r = subprocess.run([_bin(), "-y", "-v", "info", *args],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    return r.stderr


def _mean_volume_db(path, audio_filter: str = "anull") -> float:
    """mean_volume (dBFS) of `path` after `audio_filter`, via volumedetect."""
    err = _run(["-i", str(path), "-af", f"{audio_filter},volumedetect",
                "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", err)
    assert m, f"no mean_volume in ffmpeg output:\n{err[-1500:]}"
    return float(m.group(1))


def _make_clip(path, audio_hz: float | None, dur: float = 3.0) -> None:
    """A tiny mp4: black video + either a sine tone (audio_hz) or silence."""
    audio = (f"sine=frequency={audio_hz}:duration={dur}" if audio_hz
             else f"anullsrc=r=44100:cl=stereo:d={dur}")
    _run(["-f", "lavfi", "-i", f"color=c=black:s=64x64:r=10:d={dur}",
          "-f", "lavfi", "-i", audio,
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(path)])


def _make_music(path, hz: float = 220.0, dur: float = 3.0) -> None:
    # Boost to near full-scale so the source mimics a real mastered track
    # (~0 dBFS); add_music applies volume_db as a *relative* attenuation and
    # assumes a loud source, so a quiet fixture would misrepresent the mix.
    _run(["-f", "lavfi", "-i", f"sine=frequency={hz}:duration={dur}",
          "-af", "volume=18dB", str(path)])


def test_music_bed_is_audible_over_silence(tmp_path):
    """With no speech to duck against, the mixed output must carry the music
    bed near its configured level — not silence, not pinned to ~-40 dB."""
    clip = tmp_path / "clip.mp4"
    track = tmp_path / "music.wav"
    out = tmp_path / "out.mp4"
    _make_clip(clip, audio_hz=None)     # silent speech track
    _make_music(track)

    music.add_music(clip, track, out, load_config(), volume_db=-18.0)

    assert out.exists()
    mean = _mean_volume_db(out)
    # -18 dB bed, no ducking (speech is silent): comfortably above a -30 floor.
    # The old graph (base -22 stacked with the heavy duck) would sink far below.
    assert mean > -30.0, f"music bed too quiet: mean_volume {mean} dB"


def test_music_survives_ducking_under_speech(tmp_path):
    """Even under continuous speech, the gentle duck must leave the music
    band clearly present rather than crushing it inaudible."""
    clip = tmp_path / "clip.mp4"
    track = tmp_path / "music.wav"
    out = tmp_path / "out.mp4"
    _make_clip(clip, audio_hz=440.0)    # loud "speech" tone
    _make_music(track, hz=220.0)        # music one octave below

    music.add_music(clip, track, out, load_config(), volume_db=-18.0)

    # Isolate the 220 Hz music band; it must still register well above a floor.
    band = _mean_volume_db(out, audio_filter="bandpass=f=220:width_type=q:w=6")
    assert band > -40.0, f"music ducked into inaudibility: 220Hz band {band} dB"
