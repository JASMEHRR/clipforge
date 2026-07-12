"""Permanent, human-readable archive of every uploaded clip: a copy of the
video plus info.json/info.txt land in archive/uploaded/<YYYY-MM>/
<video_id>__<slug>/, independent of output/ (which "Clean up uploaded" or the
user may later delete) and independent of upload_scheduler's dedupe log
(which stays the sole upload-eligibility authority). video_id is always
unique, so it's the folder's real identity; the slug is only for humans
browsing the folder."""
from __future__ import annotations

import json
import re
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from config import ROOT, load_config
from logutil import get_logger

log = get_logger("archive")

ARCHIVE_DIR = ROOT / "archive" / "uploaded"
_SLUG_MAX = 60
ZIP_THRESHOLD = 100
_ZIP_LOCK = threading.Lock()


def _backups_dir() -> Path:
    # derived from ARCHIVE_DIR (not its own module-level constant) so tests
    # that isolate ARCHIVE_DIR automatically isolate this too — a second
    # unpatched constant is exactly how testing wrote into the real repo's
    # archive/uploaded/ during Part 2's development
    return ARCHIVE_DIR.parent / "backups"


def _manifest_path() -> Path:
    return _backups_dir() / "zip_manifest.json"


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s or "clip")[:_SLUG_MAX]


def _month_key(iso_dt: str) -> str:
    try:
        return datetime.fromisoformat(iso_dt).strftime("%Y-%m")
    except (ValueError, TypeError):
        return datetime.now().strftime("%Y-%m")


def archive_dir_for(video_id: str, title: str, uploaded_at: str) -> Path:
    return ARCHIVE_DIR / _month_key(uploaded_at) / f"{video_id}__{_slugify(title)}"


def find_archive_dir(video_id: str | None) -> Path | None:
    """The existing archive folder for this video_id, if any — video_id is
    unique, so a glob on the `<video_id>__*` prefix is unambiguous. For a
    single lookup; a caller checking many video_ids (e.g. a queue listing)
    should use index_by_video_id() instead of one glob per id."""
    if not video_id or not ARCHIVE_DIR.exists():
        return None
    matches = list(ARCHIVE_DIR.glob(f"*/{video_id}__*"))
    return matches[0] if matches else None


def index_by_video_id() -> dict[str, Path]:
    """video_id -> archive dir for every archived clip, built with one
    directory walk instead of the N globs N calls to find_archive_dir would
    cost."""
    if not ARCHIVE_DIR.exists():
        return {}
    index: dict[str, Path] = {}
    for month_dir in ARCHIVE_DIR.iterdir():
        if not month_dir.is_dir():
            continue
        for clip_dir in month_dir.iterdir():
            if clip_dir.is_dir() and "__" in clip_dir.name:
                index[clip_dir.name.split("__", 1)[0]] = clip_dir
    return index


def _write_info(target: Path, info: dict) -> None:
    (target / "info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        f"Title: {info['title']}",
        f"YouTube: {info['youtube_url']}",
        f"Video ID: {info['video_id']}",
        f"Niche: {info.get('niche') or '(none)'}",
        f"Virality score: {info.get('virality_score')}",
        f"Scheduled publish: {info.get('scheduled_publish_at') or '(immediate)'}",
        f"Uploaded: {info.get('uploaded_at') or '(unknown)'}",
        f"Source job folder: {info.get('source_job_folder')}",
        f"Tags: {' '.join(info.get('hashtags') or [])}",
        "", "Description:", info.get("description") or "(none)",
    ]
    (target / "info.txt").write_text("\n".join(lines), encoding="utf-8")


def archive_clip(video_path: Path, clip_dir: Path, video_id: str, url: str,
                 snippet: dict, *, niche: str | None, virality_score,
                 uploaded_at: str, publish_at: str | None) -> Path | None:
    """Copy `video_path` + write info.json/info.txt into
    archive/uploaded/<YYYY-MM>/<video_id>__<slug>/. `snippet` is
    {"title", "description", "hashtags"} — the same shape
    upload_scheduler.build_snippet() returns, so callers can pass it through
    unpacked. The rest are keyword-only: several are the same type
    (str | None) and a positional swap between them would silently corrupt
    info.json with no error. Idempotent (a second call for an
    already-archived video_id is a no-op) and best-effort: any I/O failure is
    logged and returns None instead of raising, so this can be called from an
    upload's success path without ever risking the upload itself."""
    if not video_id:
        return None
    existing = find_archive_dir(video_id)
    if existing:
        return existing
    title = snippet.get("title") or "Untitled"
    target = archive_dir_for(video_id, title, uploaded_at)
    try:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_path, target / "final.mp4")
        _write_info(target, {
            "youtube_url": url or f"https://youtu.be/{video_id}",
            "video_id": video_id, "title": title,
            "description": snippet.get("description") or "",
            "hashtags": snippet.get("hashtags") or [],
            "niche": niche, "virality_score": virality_score,
            "scheduled_publish_at": publish_at, "uploaded_at": uploaded_at,
            "source_job_folder": Path(clip_dir).parent.name,
        })
        return target
    except OSError as e:
        log.warning("archiving %s failed (%s); the output/ copy stays the "
                    "only one for now", video_id, e)
        shutil.rmtree(target, ignore_errors=True)  # no half-written folder left behind
        return None


def ensure_archived(key: str, entry: dict) -> Path | None:
    """Archive this upload_log entry's clip if it isn't archived yet, reading
    whatever's currently in its output/ clip folder (metadata.json + the
    current final.mp4 — NOT necessarily the exact watermarked file that was
    uploaded; that temp copy is deleted right after upload, so only the
    live archive_clip() call made at upload time can capture it exactly).
    Returns the archive dir, or None if it can't be archived — no video_id
    on the log entry, or the clip's files are already gone from disk."""
    video_id = entry.get("video_id")
    existing = find_archive_dir(video_id)
    if existing:
        return existing
    if not video_id:
        return None
    clip_dir = (ROOT / key).resolve()
    output_root = (ROOT / load_config()["paths"]["output_dir"]).resolve()
    video_path = clip_dir / "final.mp4"
    if not clip_dir.is_relative_to(output_root) or not video_path.is_file():
        return None
    meta = {}
    meta_path = clip_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    snippet = {"title": entry.get("title") or meta.get("title", ""),
              "description": meta.get("description", ""),
              "hashtags": meta.get("hashtags", [])}
    return archive_clip(
        video_path, clip_dir, video_id, f"https://youtu.be/{video_id}", snippet,
        niche=meta.get("niche"),
        virality_score=(meta.get("virality") or {}).get(
            "score", entry.get("virality_score")),
        uploaded_at=entry.get("uploaded_at", ""), publish_at=entry.get("publish_at"))


def backfill_from_log(log_data: dict) -> dict:
    """One-time sweep: archive everything already in upload_log.json that
    isn't archived yet and still has files on disk. Returns
    {"archived": n, "skipped": n} — skipped covers both already-archived and
    unrecoverable (files deleted) entries; callers don't need to tell those
    apart."""
    archived = skipped = 0
    for key, entry in log_data.get("uploads", {}).items():
        already = find_archive_dir(entry.get("video_id"))
        result = already or ensure_archived(key, entry)
        if result and not already:
            archived += 1
        else:
            skipped += 1
    return {"archived": archived, "skipped": skipped}


# ============================================================
# Zip backups
# ============================================================
def _load_manifest() -> dict:
    path = _manifest_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = path.with_suffix(".json.corrupt")
            try:
                path.replace(backup)  # overwrites a stale .corrupt from a
            except OSError as e:      # prior event instead of raising on it
                log.warning("could not quarantine corrupt %s (%s); "
                           "deleting it instead", path.name, e)
                path.unlink(missing_ok=True)
            else:
                log.warning("%s was corrupt; moved to %s, starting fresh",
                           path.name, backup.name)
    return {"zips": {}}


def _save_manifest(data: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic on the same drive; no half-written manifest


def zipped_video_ids(manifest: dict | None = None) -> set[str]:
    manifest = manifest if manifest is not None else _load_manifest()
    ids: set[str] = set()
    for z in manifest.get("zips", {}).values():
        ids.update(z.get("clips", []))
    return ids


def find_zip_for(video_id: str | None) -> str | None:
    """Which backup zip (if any) holds this video_id — lets the Uploaded
    panel say "in backup <name>" for a clip whose archive/uploaded/ folder
    was deleted after being zipped (the delete-originals opt-in)."""
    if not video_id:
        return None
    for name, z in _load_manifest().get("zips", {}).items():
        if video_id in z.get("clips", []):
            return name
    return None


def zip_status() -> dict:
    """How many archived clips aren't in any backup zip yet, and whether
    that's enough to prompt for a new one."""
    since_last_zip = len(set(index_by_video_id()) - zipped_video_ids())
    return {"since_last_zip": since_last_zip, "threshold": ZIP_THRESHOLD,
            "should_prompt": since_last_zip >= ZIP_THRESHOLD}


def create_backup_zip(on_progress=None, delete_originals: bool = False) -> dict:
    """Zip every archived clip not already covered by a previous backup into
    archive/backups/clipforge_backup_<date>_<n>of<n>.zip (n = how many backup
    zips exist once this one is written). Streams each file straight from
    disk via ZipFile.write() (never holds a whole clip's bytes in memory);
    after closing, reopens and verifies member count + testzip() before the
    manifest is updated or anything is deleted — a failed verify removes the
    broken zip and changes nothing else. `on_progress(done, total)` fires
    after each clip. Returns {"zipped": 0} with no file created if there's
    nothing new to back up. Serialized by _ZIP_LOCK: two callers racing here
    (e.g. the zip-prompt banner and the manual button both clicked) would
    otherwise compute the same output filename and the second ZipFile("w")
    would truncate the first one's in-progress zip."""
    with _ZIP_LOCK:
        manifest = _load_manifest()
        already = zipped_video_ids(manifest)
        to_zip = {vid: d for vid, d in index_by_video_id().items()
                 if vid not in already}
        if not to_zip:
            return {"zipped": 0, "zip_name": None}

        backups_dir = _backups_dir()
        backups_dir.mkdir(parents=True, exist_ok=True)
        n = len(manifest.get("zips", {})) + 1
        date = datetime.now().strftime("%Y-%m-%d")
        name = f"clipforge_backup_{date}_{n}of{n}.zip"
        path = backups_dir / name

        items = sorted(to_zip.items())
        files_written = 0
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, (video_id, clip_dir) in enumerate(items, start=1):
                    for f in sorted(clip_dir.iterdir()):
                        if f.is_file():
                            zf.write(f, f"{clip_dir.name}/{f.name}")
                            files_written += 1
                    if on_progress:
                        on_progress(i, len(items))
        except OSError as e:
            log.warning("zip backup failed (%s); no manifest change", e)
            path.unlink(missing_ok=True)
            return {"zipped": 0, "zip_name": None, "error": str(e)}

        try:
            with zipfile.ZipFile(path) as zf:
                bad_member = zf.testzip()
                member_count = len(zf.namelist())
        except (OSError, zipfile.BadZipFile) as e:
            log.warning("zip verify failed (%s); removing broken zip", e)
            path.unlink(missing_ok=True)
            return {"zipped": 0, "zip_name": None, "error": str(e)}

        if bad_member is not None or member_count != files_written:
            log.warning("zip verify mismatch (bad=%s, %d/%d members); removing %s",
                        bad_member, member_count, files_written, name)
            path.unlink(missing_ok=True)
            return {"zipped": 0, "zip_name": None,
                    "error": "The backup zip didn't verify — try again."}

        manifest.setdefault("zips", {})[name] = {
            "created_at": datetime.now().isoformat(),
            "clips": sorted(to_zip.keys()), "count": len(to_zip),
        }
        _save_manifest(manifest)

        if delete_originals:
            for clip_dir in to_zip.values():
                shutil.rmtree(clip_dir, ignore_errors=True)

        return {"zipped": len(to_zip), "zip_name": name, "zip_path": str(path)}


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ARCHIVE_DIR = tmp / "archive" / "uploaded"  # module-level override for the smoke test
        clip_dir = tmp / "output" / "job1" / "clip_00"
        clip_dir.mkdir(parents=True)
        video = clip_dir / "final.mp4"
        video.write_bytes(b"\x00" * 16)

        d = archive_clip(
            video, clip_dir, "vid123", "https://youtu.be/vid123",
            {"title": "A Great Title!", "description": "desc here",
             "hashtags": ["#a", "#shorts"]}, niche="gaming", virality_score=77,
            uploaded_at="2026-07-12T10:00:00+05:30", publish_at=None)
        assert d is not None and (d / "final.mp4").is_file()
        assert (d / "info.json").is_file() and (d / "info.txt").is_file()
        assert d == find_archive_dir("vid123")
        assert index_by_video_id() == {"vid123": d}
        # idempotent: a second call for the same video_id is a no-op, not a
        # second folder
        assert archive_clip(video, clip_dir, "vid123", "", {}, niche=None,
                            virality_score=None, uploaded_at="",
                            publish_at=None) == d
        print("archive.py self-check OK:", d)
