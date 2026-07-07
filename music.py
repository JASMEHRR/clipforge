"""Copyright-free background music: pick a verified-license track, duck it under
speech, and mix it into a finished clip.

Tracks live in assets/music/manifest.json — each records its license and, when
the license requires it, the attribution string ClipForge appends to the clip
description. Track audio is downloaded on first use from its source_url and
cached next to the manifest (the mp3s are gitignored).

Mixing (ffmpeg): the music loops/trims to the clip length, gets 1 s in/out
fades, is attenuated to `volume_db` (default -22 dB), and is side-chain ducked
under the speech so dialogue stays clear."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from config import ROOT, load_config
from errors import ClipForgeError
from ffutil import probe, run_ffmpeg
from logutil import get_logger

log = get_logger("music")

MUSIC_DIR = ROOT / "assets" / "music"
MANIFEST_PATH = MUSIC_DIR / "manifest.json"
DEFAULT_VOLUME_DB = -22.0
_USER_AGENT = "ClipForge/2.0 (+https://github.com/; background-music fetch)"
_download_lock = threading.Lock()


class MusicError(ClipForgeError):
    stage = "music"


def load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        raise MusicError(f"music manifest not found: {MANIFEST_PATH}")
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return data.get("tracks", [])


def list_tracks() -> list[dict]:
    """Manifest tracks (id/title/license/… — no audio downloaded)."""
    return load_manifest()


def get_track(track_id: str) -> dict:
    for t in load_manifest():
        if t["id"] == track_id:
            return t
    raise MusicError(f"unknown music track '{track_id}'")


def pick_track_by_mood(text: str) -> dict:
    """Auto-match: score each track by how many of its mood words appear in the
    transcript; ties and misses fall back to the 'default' mood track."""
    tracks = load_manifest()
    if not tracks:
        raise MusicError("music manifest is empty")
    words = {w.lower().strip(".,!?\"'():;") for w in (text or "").split()}
    best, best_score = None, -1
    for t in tracks:
        score = sum(1 for m in t.get("moods", []) if m in words)
        if score > best_score:
            best, best_score = t, score
    if best_score <= 0:
        best = next((t for t in tracks if "default" in t.get("moods", [])),
                    tracks[0])
    log.info("auto-matched music: %s (mood score %d)", best["id"], best_score)
    return best


def ensure_track(track: dict) -> Path:
    """Return the local mp3 path, downloading it on first use. Thread-safe so
    parallel clip workers don't race on the same file."""
    dest = MUSIC_DIR / track["file"]
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    url = track["source_url"]
    with _download_lock:
        if dest.exists() and dest.stat().st_size > 10_000:  # won while waiting
            return dest
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        log.info("downloading music '%s' (%s) from %s", track["id"],
                 track["license"], url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(tmp, "wb") as f:
                f.write(resp.read())
        except Exception as e:  # noqa: BLE001 — network/URL errors are non-fatal
            tmp.unlink(missing_ok=True)
            raise MusicError(f"music download failed for '{track['id']}'",
                             detail=str(e)[:300]) from e
        if tmp.stat().st_size <= 10_000:
            tmp.unlink(missing_ok=True)
            raise MusicError(f"music download too small for '{track['id']}'")
        tmp.replace(dest)
    return dest


def attribution_for(track: dict) -> str:
    """Attribution string to append to a clip description, or '' if the license
    doesn't require attribution."""
    return track.get("attribution", "") if track.get("attribution_required") else ""


def add_music(clip_path: str | Path, music_path: str | Path,
              out_path: str | Path, cfg: dict | None = None,
              volume_db: float = DEFAULT_VOLUME_DB) -> Path:
    """Mix a backing track into a finished clip: loop/trim to length, 1 s fades,
    side-chain duck under speech. Video is stream-copied; audio re-encoded."""
    cfg = cfg or load_config()
    clip_path, music_path, out_path = (Path(clip_path), Path(music_path),
                                       Path(out_path))
    if not clip_path.exists():
        raise MusicError(f"clip not found: {clip_path}")
    if not music_path.exists():
        raise MusicError(f"music not found: {music_path}")

    dur = probe(clip_path)["duration"]
    fout = max(0.0, dur - 1.0)
    graph = (
        "[0:a]asplit=2[sc][spk];"
        f"[1:a]atrim=0:{dur:.3f},asetpts=N/SR/TB,volume={volume_db:.1f}dB,"
        f"afade=t=in:st=0:d=1,afade=t=out:st={fout:.3f}:d=1[m];"
        # duck the music using the speech as the side-chain key
        "[m][sc]sidechaincompress=threshold=0.02:ratio=8:attack=15:"
        "release=300[mduck];"
        "[spk][mduck]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    run_ffmpeg(["-i", clip_path, "-stream_loop", "-1", "-i", music_path,
                "-filter_complex", graph, "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a",
                cfg["render"]["audio_bitrate"], "-movflags", "+faststart",
                out_path])
    log.info("music mixed (%.1f dB, ducked) -> %s", volume_db, out_path.name)
    return out_path


def resolve(choice: str | None, transcript_text: str) -> dict | None:
    """Map a UI/CLI choice ('' / None -> off, 'auto' -> mood match, else a track
    id) to a manifest track, or None for no music."""
    if not choice:
        return None
    if choice == "auto":
        return pick_track_by_mood(transcript_text)
    return get_track(choice)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="smoke: list tracks / mix music")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--clip")
    ap.add_argument("--track", default="local_forecast_elevator")
    ap.add_argument("--out", default="output/_smoke_music.mp4")
    a = ap.parse_args()
    if a.list or not a.clip:
        for t in list_tracks():
            print(f"{t['id']:24} {t['license']:10} moods={t['moods']}")
    else:
        tr = get_track(a.track)
        print(add_music(a.clip, ensure_track(tr), a.out))
        print("attribution:", attribution_for(tr) or "(none required)")
