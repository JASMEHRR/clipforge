"""Copyright-free background music + final audio mixdown.

Tracks live in assets/music/manifest.json — each records its license and, when
the license requires it, the attribution string ClipForge appends to the clip
description. Track audio is downloaded on first use from its source_url and
cached next to the manifest (the mp3s are gitignored).

Mix chain (`mix_audio`, one ffmpeg filter_complex, fixed order):
voice → SFX overlays (per-cue adelay) → music bed side-chain ducked under the
voice → loudnorm to -14 LUFS / -1 dBTP (platform standard, single-pass;
two-pass measure+apply is the upgrade path if precision matters). The music
loops/trims to the clip length, gets 1 s in/out fades, and is attenuated to
`volume_db` (default -18 dB) before the duck.

SFX packs live in assets/sfx/<pack>/manifest.json (kind → file). The default
pack's audio files are synthesized locally with ffmpeg on first use, so the
feature works keyless with no bundled binaries."""
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
SFX_DIR = ROOT / "assets" / "sfx"
DEFAULT_VOLUME_DB = -18.0
# Duck defaults — tuned values documented in build_mix_graph; overridable via
# config music.duck.*.
DEFAULT_DUCK = {"threshold": 0.05, "ratio": 3, "attack": 15, "release": 200}
DEFAULT_LOUDNORM = {"enabled": True, "i": -14, "tp": -1, "lra": 11}
SFX_KINDS = ("whoosh", "pop", "ding", "riser")
_USER_AGENT = "ClipForge/2.0 (+https://github.com/; background-music fetch)"
_download_lock = threading.Lock()
_sfx_lock = threading.Lock()


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


def check_library(path: str | Path, cfg: dict | None = None) -> str | None:
    """Warn (message, non-fatal) when a music file lives outside the designated
    royalty-free library folder (config music.library_dir). Only library tracks
    have verified licenses; anything else is the user's own risk."""
    cfg = cfg or load_config()
    lib_rel = cfg.get("music", {}).get("library_dir", "assets/music")
    lib = (ROOT / lib_rel).resolve()
    try:
        resolved = Path(path).resolve()
    except OSError:
        return f"music track path could not be resolved: {path}"
    if lib == resolved or lib in resolved.parents:
        return None
    return (f"music track '{Path(path).name}' is outside the royalty-free "
            f"library folder ({lib_rel}) — license unverified")


# --- SFX packs ----------------------------------------------------------------

# Default pack sounds, synthesized locally with ffmpeg lavfi so the pack works
# keyless with no bundled audio binaries. Users can drop their own wavs into
# assets/sfx/<pack>/ and point the manifest at them.
_SFX_SYNTH = {
    "whoosh": "anoisesrc=d=0.4:c=pink:a=0.6,afade=t=in:st=0:d=0.15,"
              "afade=t=out:st=0.2:d=0.2",
    "pop": "sine=frequency=880:duration=0.12,afade=t=out:st=0.02:d=0.1",
    "ding": "sine=frequency=1320:duration=0.5,afade=t=out:st=0.05:d=0.45",
    "riser": "anoisesrc=d=0.8:c=pink:a=0.5,afade=t=in:st=0:d=0.7,"
             "afade=t=out:st=0.7:d=0.1",
}


def load_sfx_manifest(pack: str) -> dict:
    """kind → filename map for a pack; the default pack is created on demand."""
    pack_dir = SFX_DIR / pack
    manifest = pack_dir / "manifest.json"
    if not manifest.exists():
        if pack != "default":
            raise MusicError(f"sfx pack not found: {pack}")
        pack_dir.mkdir(parents=True, exist_ok=True)
        data = {kind: f"{kind}.wav" for kind in SFX_KINDS}
        manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MusicError(f"sfx manifest malformed: {manifest}")
    return data


def ensure_sfx(pack: str, kind: str) -> Path | None:
    """Local path for one SFX sound, synthesizing default-pack sounds on first
    use. Returns None (cue skipped, logged) when the sound can't be provided —
    SFX are decoration and must never fail a render."""
    try:
        manifest = load_sfx_manifest(pack)
    except MusicError as e:
        log.warning("sfx pack unavailable: %s", e)
        return None
    fname = manifest.get(kind)
    if not fname:
        log.warning("sfx pack '%s' has no sound for kind '%s'", pack, kind)
        return None
    dest = SFX_DIR / pack / fname
    if dest.exists():
        return dest
    if pack == "default" and kind in _SFX_SYNTH:
        with _sfx_lock:
            if dest.exists():  # won while waiting
                return dest
            try:
                run_ffmpeg(["-f", "lavfi", "-i", _SFX_SYNTH[kind], dest])
            except Exception as e:  # noqa: BLE001 — sfx are best-effort
                log.warning("sfx synth failed for '%s': %s", kind, e)
                return None
        return dest
    log.warning("sfx file missing: %s", dest)
    return None


def resolve_sfx_cues(sfx_cues: list[dict] | None, cfg: dict) -> list[dict]:
    """Cue dicts ({t, kind}) → renderable entries ({t, path}) honoring the
    sfx config gate, pack resolution, and the per-clip cap."""
    sfx_cfg = cfg.get("sfx", {})
    if not sfx_cues or not sfx_cfg.get("enabled", False):
        return []
    pack = sfx_cfg.get("pack", "default")
    cap = int(sfx_cfg.get("max_per_clip", 8))
    out = []
    for cue in sorted(sfx_cues, key=lambda c: c["t"])[:cap]:
        path = ensure_sfx(pack, cue["kind"])
        if path is not None:
            out.append({"t": float(cue["t"]), "path": path})
    return out


# --- mixdown -------------------------------------------------------------------

def mix_audio(clip_path: str | Path, out_path: str | Path,
              cfg: dict | None = None, music_path: str | Path | None = None,
              volume_db: float = DEFAULT_VOLUME_DB,
              sfx_cues: list[dict] | None = None) -> Path | None:
    """Final audio mixdown for a rendered clip: SFX overlays at cue times, the
    ducked music bed, then -14 LUFS / -1 dBTP normalization. Video is
    stream-copied; audio re-encoded. Returns None when there is nothing to do
    (no music, no cues, loudnorm disabled)."""
    cfg = cfg or load_config()
    clip_path, out_path = Path(clip_path), Path(out_path)
    if not clip_path.exists():
        raise MusicError(f"clip not found: {clip_path}")

    loudnorm = {**DEFAULT_LOUDNORM, **cfg.get("music", {}).get("loudnorm", {})}
    sfx = resolve_sfx_cues(sfx_cues, cfg)
    if music_path is not None:
        music_path = Path(music_path)
        if not music_path.exists():
            raise MusicError(f"music not found: {music_path}")
        lib_warn = check_library(music_path, cfg)
        if lib_warn:
            log.warning("%s", lib_warn)
    if music_path is None and not sfx and not loudnorm["enabled"]:
        return None

    dur = probe(clip_path)["duration"]
    graph, extra_inputs = _mix_graph(dur, cfg, sfx=sfx,
                                     has_music=music_path is not None,
                                     volume_db=volume_db, loudnorm=loudnorm)
    args: list = ["-i", clip_path]
    for entry in sfx:
        args += ["-i", entry["path"]]
    if music_path is not None:
        args += ["-stream_loop", "-1", "-i", music_path]
    run_ffmpeg([*args, "-filter_complex", graph, "-map", "0:v",
                "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a",
                cfg["render"]["audio_bitrate"], "-movflags", "+faststart",
                out_path])
    log.info("audio mixdown (%d sfx, music=%s, loudnorm=%s) -> %s",
             len(sfx), music_path is not None, loudnorm["enabled"],
             out_path.name)
    return out_path


def _mix_graph(dur: float, cfg: dict, *, sfx: list[dict], has_music: bool,
               volume_db: float, loudnorm: dict) -> tuple[str, int]:
    """Assemble the filter_complex string. Pure given resolved inputs.

    Input layout: 0 = clip audio, 1..len(sfx) = SFX files (in list order),
    last = music (when has_music). Chain order is the fix for the reported
    "music drowns the voice / clip sounds quiet" problem: voice → SFX →
    side-chain-ducked music → loudnorm to platform loudness."""
    parts: list[str] = []
    voice = "[0:a]"

    if sfx:
        sfx_cfg = cfg.get("sfx", {})
        gain = float(sfx_cfg.get("volume_db", -10.0))
        labels = []
        for n, entry in enumerate(sfx, start=1):
            ms = max(0, int(round(entry["t"] * 1000)))
            parts.append(f"[{n}:a]volume={gain:.1f}dB,"
                         f"adelay={ms}:all=1[sfx{n}]")
            labels.append(f"[sfx{n}]")
        parts.append(f"{voice}{''.join(labels)}"
                     f"amix=inputs={len(labels) + 1}:duration=first:"
                     "normalize=0[vx]")
        voice = "[vx]"

    if has_music:
        duck = {**DEFAULT_DUCK, **cfg.get("music", {}).get("duck", {})}
        m_idx = len(sfx) + 1
        fout = max(0.0, dur - 1.0)
        parts.append(f"{voice}asplit=2[sc][spk]")
        parts.append(
            f"[{m_idx}:a]atrim=0:{dur:.3f},asetpts=N/SR/TB,"
            f"volume={volume_db:.1f}dB,"
            f"afade=t=in:st=0:d=1,afade=t=out:st={fout:.3f}:d=1[m]")
        # Duck the music under speech, gently: threshold ~-26 dB (0.05) so
        # only real speech triggers it, ratio 3 and a quick 200 ms release so
        # the bed dips a few dB and recovers between words instead of being
        # pinned inaudible. (Was threshold=0.02:ratio=8:release=300, which
        # stacked with the base attenuation to leave the bed ~-40 dB.)
        parts.append(
            f"[m][sc]sidechaincompress=threshold={duck['threshold']}:"
            f"ratio={duck['ratio']}:attack={duck['attack']}:"
            f"release={duck['release']}[mduck]")
        parts.append("[spk][mduck]amix=inputs=2:duration=first:"
                     "normalize=0[mix]")
        voice = "[mix]"

    if loudnorm["enabled"]:
        # Single-pass loudnorm to the platform standard; aresample back to
        # 48 kHz because loudnorm internally upsamples to 192 kHz.
        parts.append(
            f"{voice}loudnorm=I={loudnorm['i']}:TP={loudnorm['tp']}:"
            f"LRA={loudnorm['lra']},aresample=48000[aout]")
    else:
        parts.append(f"{voice}anull[aout]")

    return ";".join(parts), len(sfx) + (1 if has_music else 0)


def add_music(clip_path: str | Path, music_path: str | Path,
              out_path: str | Path, cfg: dict | None = None,
              volume_db: float = DEFAULT_VOLUME_DB) -> Path:
    """Back-compat wrapper: music-only mixdown (now includes loudnorm)."""
    result = mix_audio(clip_path, out_path, cfg, music_path=music_path,
                       volume_db=volume_db)
    assert result is not None  # music_path given → always renders
    return result


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
