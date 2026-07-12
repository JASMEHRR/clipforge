"""archive.py — every operation is disk I/O, so every test uses a real
tmp_path tree and asserts nothing touches anything outside it."""
import json
from pathlib import Path

import pytest

import archive


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "archive" / "uploaded")
    return tmp_path


def _clip(tmp_path, job="job1", clip="clip_00", title="Title"):
    clip_dir = tmp_path / "output" / job / clip
    clip_dir.mkdir(parents=True)
    (clip_dir / "final.mp4").write_bytes(b"\x00" * 32)
    (clip_dir / "metadata.json").write_text(json.dumps({
        "title": title, "description": "A description.",
        "hashtags": ["#a", "#shorts"], "niche": "gaming",
        "virality": {"score": 61},
    }), encoding="utf-8")
    return clip_dir


def _snippet(title="Title", description="", hashtags=None):
    return {"title": title, "description": description, "hashtags": hashtags or []}


def test_archive_clip_copies_video_and_writes_info(tmp_path):
    clip_dir = _clip(tmp_path)
    d = archive.archive_clip(
        clip_dir / "final.mp4", clip_dir, "vid1", "https://youtu.be/vid1",
        _snippet("My Title", "desc", ["#a", "#shorts"]),
        niche="gaming", virality_score=61,
        uploaded_at="2026-07-12T10:00:00+05:30",
        publish_at="2026-07-12T17:00:00+05:30")

    assert d is not None
    assert (d / "final.mp4").is_file()
    assert (d / "final.mp4").read_bytes() == (clip_dir / "final.mp4").read_bytes()
    # the original output/ copy is untouched (archive is a copy, not a move)
    assert (clip_dir / "final.mp4").exists()

    info = json.loads((d / "info.json").read_text())
    assert info["video_id"] == "vid1"
    assert info["youtube_url"] == "https://youtu.be/vid1"
    assert info["niche"] == "gaming" and info["virality_score"] == 61
    assert info["source_job_folder"] == "job1"
    assert "My Title" in (d / "info.txt").read_text()


def test_immediate_publish_records_no_fake_scheduled_time(tmp_path):
    """upload_now (immediate/public) passes publish_at=None — info.json must
    say so, not repeat uploaded_at as a fabricated scheduled slot."""
    clip_dir = _clip(tmp_path)
    d = archive.archive_clip(
        clip_dir / "final.mp4", clip_dir, "vidImm", "u", _snippet("T"),
        niche=None, virality_score=None,
        uploaded_at="2026-07-12T10:00:00+05:30", publish_at=None)
    info = json.loads((d / "info.json").read_text())
    assert info["scheduled_publish_at"] is None
    assert "(immediate)" in (d / "info.txt").read_text()


def test_archive_dir_named_by_month_and_prefixed_by_video_id(tmp_path):
    clip_dir = _clip(tmp_path)
    d = archive.archive_clip(
        clip_dir / "final.mp4", clip_dir, "vid2", "u",
        _snippet("Some: Weird/Title!!"), niche=None, virality_score=None,
        uploaded_at="2026-01-05T09:00:00+05:30", publish_at=None)
    assert d.parent.name == "2026-01"
    assert d.name.startswith("vid2__")
    assert "/" not in d.name and ":" not in d.name  # slug is filesystem-safe


def test_archive_clip_is_idempotent_per_video_id(tmp_path):
    clip_dir = _clip(tmp_path)
    d1 = archive.archive_clip(clip_dir / "final.mp4", clip_dir, "vid3", "u",
                              _snippet("T"), niche=None, virality_score=None,
                              uploaded_at="2026-07-12T00:00:00", publish_at=None)
    d2 = archive.archive_clip(clip_dir / "final.mp4", clip_dir, "vid3", "u",
                              _snippet("Different title now"), niche=None,
                              virality_score=None,
                              uploaded_at="2026-07-12T00:00:00", publish_at=None)
    assert d1 == d2
    assert len(list(archive.ARCHIVE_DIR.glob("*/vid3__*"))) == 1


def test_two_clips_same_title_get_distinct_folders_via_video_id(tmp_path):
    clip_dir = _clip(tmp_path, clip="clip_00", title="Same Title")
    d1 = archive.archive_clip(clip_dir / "final.mp4", clip_dir, "vidA", "u",
                              _snippet("Same Title"), niche=None,
                              virality_score=None,
                              uploaded_at="2026-07-12T00:00:00", publish_at=None)
    d2 = archive.archive_clip(clip_dir / "final.mp4", clip_dir, "vidB", "u",
                              _snippet("Same Title"), niche=None,
                              virality_score=None,
                              uploaded_at="2026-07-12T00:00:00", publish_at=None)
    assert d1 != d2
    assert d1.exists() and d2.exists()


def test_find_archive_dir_missing_video_returns_none(tmp_path):
    assert archive.find_archive_dir("nope") is None
    assert archive.find_archive_dir(None) is None
    assert archive.find_archive_dir("") is None


def test_index_by_video_id_matches_find_archive_dir(tmp_path):
    clip_dir = _clip(tmp_path)
    d1 = archive.archive_clip(clip_dir / "final.mp4", clip_dir, "vidI1", "u",
                              _snippet("One"), niche=None, virality_score=None,
                              uploaded_at="2026-07-12T00:00:00", publish_at=None)
    d2 = archive.archive_clip(clip_dir / "final.mp4", clip_dir, "vidI2", "u",
                              _snippet("Two"), niche=None, virality_score=None,
                              uploaded_at="2026-08-01T00:00:00", publish_at=None)

    assert archive.index_by_video_id() == {"vidI1": d1, "vidI2": d2}


def test_index_by_video_id_empty_when_nothing_archived(tmp_path):
    assert archive.index_by_video_id() == {}


def _isolate_output_root(tmp_path, monkeypatch):
    """ensure_archived/backfill_from_log sandbox against
    ROOT / load_config()['paths']['output_dir'] — point both at tmp_path
    without ever asking load_config() to read a config.yaml that doesn't
    exist there (read the real config first, then swap ROOT under it)."""
    import config as config_mod
    cfg = dict(config_mod.load_config())
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(archive, "ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "_cached",
                        {**cfg, "paths": {**cfg["paths"], "output_dir": "output"}})


def test_ensure_archived_reads_metadata_and_archives_from_output(tmp_path, monkeypatch):
    _isolate_output_root(tmp_path, monkeypatch)
    _clip(tmp_path)

    entry = {"video_id": "vidZ", "uploaded_at": "2026-07-12T10:00:00",
             "publish_at": "2026-07-12T17:00:00", "title": "Log title"}
    d = archive.ensure_archived("output/job1/clip_00", entry)
    assert d is not None and (d / "final.mp4").is_file()
    info = json.loads((d / "info.json").read_text())
    assert info["niche"] == "gaming"  # pulled from metadata.json, not the log


def test_ensure_archived_returns_none_when_files_already_gone(tmp_path, monkeypatch):
    _isolate_output_root(tmp_path, monkeypatch)
    entry = {"video_id": "vidGone", "uploaded_at": "2026-07-12T10:00:00"}
    assert archive.ensure_archived("output/job1/clip_99", entry) is None


def test_ensure_archived_without_video_id_returns_none(tmp_path):
    assert archive.ensure_archived("output/job1/clip_00", {}) is None


def test_backfill_from_log_archives_new_and_skips_rest(tmp_path, monkeypatch):
    _isolate_output_root(tmp_path, monkeypatch)
    _clip(tmp_path, job="job1", clip="clip_00", title="Fresh")
    already_dir = archive.archive_clip(
        _clip(tmp_path, job="job2", clip="clip_00", title="Old") / "final.mp4",
        tmp_path / "output" / "job2" / "clip_00", "vidOld", "u",
        _snippet("Old"), niche=None, virality_score=None,
        uploaded_at="2026-06-01T00:00:00", publish_at=None)

    log_data = {"uploads": {
        "output/job1/clip_00": {"video_id": "vidNew",
                                "uploaded_at": "2026-07-12T10:00:00"},
        "output/job2/clip_00": {"video_id": "vidOld",
                                "uploaded_at": "2026-06-01T00:00:00"},
        "output/job3/clip_00": {"video_id": "vidMissing",
                                "uploaded_at": "2026-07-01T00:00:00"},
    }}
    result = archive.backfill_from_log(log_data)
    assert result == {"archived": 1, "skipped": 2}
    assert archive.find_archive_dir("vidNew") is not None
    assert archive.find_archive_dir("vidOld") == already_dir


# ============================================================
# Zip backups (Part 3)
# ============================================================
def _archive_n_clips(tmp_path, n, prefix="vid"):
    ids = []
    for i in range(n):
        video_id = f"{prefix}{i}"
        clip_dir = _clip(tmp_path, job=f"job-{prefix}{i}", clip="clip_00",
                         title=f"Clip {i}")
        archive.archive_clip(clip_dir / "final.mp4", clip_dir, video_id, "u",
                             _snippet(f"Clip {i}"), niche="gaming",
                             virality_score=i, uploaded_at="2026-07-12T00:00:00",
                             publish_at=None)
        ids.append(video_id)
    return ids


def test_zip_status_below_and_at_threshold(tmp_path):
    _archive_n_clips(tmp_path, 3)
    below = archive.zip_status()
    assert below == {"since_last_zip": 3, "threshold": archive.ZIP_THRESHOLD,
                     "should_prompt": False}

    import archive as archive_mod
    prev = archive_mod.ZIP_THRESHOLD
    archive_mod.ZIP_THRESHOLD = 3
    try:
        assert archive.zip_status()["should_prompt"] is True
    finally:
        archive_mod.ZIP_THRESHOLD = prev


def test_create_backup_zip_streams_verifies_and_updates_manifest(tmp_path):
    ids = _archive_n_clips(tmp_path, 3)
    progress = []
    result = archive.create_backup_zip(on_progress=lambda d, t: progress.append((d, t)))

    assert result["zipped"] == 3
    zip_path = Path(result["zip_path"])
    assert zip_path.name == result["zip_name"]
    assert zip_path.is_file()
    assert progress == [(1, 3), (2, 3), (3, 3)]

    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        # 3 files per clip (final.mp4, info.json, info.txt) x 3 clips
        assert len(names) == 9
        assert any(n.endswith("final.mp4") for n in names)

    # originals are untouched by default (delete_originals defaults to False)
    for vid in ids:
        assert archive.find_archive_dir(vid) is not None

    manifest = archive._load_manifest()
    assert set(manifest["zips"][result["zip_name"]]["clips"]) == set(ids)
    assert archive.zipped_video_ids() == set(ids)
    assert archive.zip_status()["since_last_zip"] == 0


def test_create_backup_zip_is_idempotent_only_zips_whats_new(tmp_path):
    _archive_n_clips(tmp_path, 2)
    first = archive.create_backup_zip()
    assert first["zipped"] == 2

    again = archive.create_backup_zip()
    assert again == {"zipped": 0, "zip_name": None}

    new_ids = _archive_n_clips(tmp_path, 1, prefix="fresh")
    second = archive.create_backup_zip()
    assert second["zipped"] == 1
    assert second["zip_name"] != first["zip_name"]
    assert archive.find_zip_for(new_ids[0]) == second["zip_name"]


def test_create_backup_zip_delete_originals_removes_source_folders(tmp_path):
    ids = _archive_n_clips(tmp_path, 2)
    result = archive.create_backup_zip(delete_originals=True)
    assert result["zipped"] == 2
    for vid in ids:
        assert archive.find_archive_dir(vid) is None       # folder gone
        assert archive.find_zip_for(vid) == result["zip_name"]  # but still tracked


def test_create_backup_zip_nothing_to_zip_creates_no_file(tmp_path):
    result = archive.create_backup_zip()
    assert result == {"zipped": 0, "zip_name": None}
    backups = archive._backups_dir()
    assert not backups.exists() or not list(backups.glob("*.zip"))


def test_create_backup_zip_removes_broken_zip_and_skips_manifest(tmp_path, monkeypatch):
    """A verify failure (testzip finds a bad member) must not update the
    manifest or leave the broken zip behind — the clip stays un-backed-up so
    the next attempt retries it, not silently marks it done."""
    ids = _archive_n_clips(tmp_path, 1)
    import zipfile as zipfile_mod
    monkeypatch.setattr(zipfile_mod.ZipFile, "testzip", lambda self: "bad/member")

    result = archive.create_backup_zip()
    assert result["zipped"] == 0 and "error" in result
    assert archive.zipped_video_ids() == set()
    assert not list(archive._backups_dir().glob("*.zip"))
    assert archive.find_archive_dir(ids[0]) is not None  # original untouched


def test_find_zip_for_unarchived_video_returns_none(tmp_path):
    assert archive.find_zip_for("nope") is None
    assert archive.find_zip_for(None) is None


def test_load_manifest_survives_a_preexisting_corrupt_backup(tmp_path):
    """A second corruption event must not crash on Path.rename raising
    FileExistsError against a .corrupt file left over from a first one."""
    path = archive._manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".json.corrupt").write_text("stale", encoding="utf-8")
    path.write_text("{not json", encoding="utf-8")

    manifest = archive._load_manifest()
    assert manifest == {"zips": {}}
    assert path.with_suffix(".json.corrupt").read_text(encoding="utf-8") == "{not json"


def test_create_backup_zip_concurrent_calls_dont_corrupt_or_duplicate(tmp_path):
    """Two threads racing create_backup_zip must not compute the same
    filename and truncate each other's zip, and every archived clip must end
    up in exactly one zip afterward."""
    import threading
    ids = _archive_n_clips(tmp_path, 6)
    results = []
    errors = []

    def worker():
        try:
            results.append(archive.create_backup_zip())
        except Exception as e:  # noqa: BLE001 — surface any crash to the test
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    zipped_total = sum(r["zipped"] for r in results)
    assert zipped_total == len(ids)  # no clip zipped twice, none dropped

    manifest = archive._load_manifest()
    all_manifest_clips = [c for z in manifest["zips"].values() for c in z["clips"]]
    assert sorted(all_manifest_clips) == sorted(ids)  # no duplicates, none lost
    for vid in ids:
        assert archive.find_zip_for(vid) is not None
