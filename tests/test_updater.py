"""Self-updater apply/rollback, exercised in a sandbox tmp repo.

updater.ROOT / UPDATES_DIR are module globals resolved at call time, so
monkeypatching them redirects every read/write into tmp_path — the real working
repo is never touched. Network is stubbed; no real GitHub calls here."""
import updater


def _fake_repo(tmp_path):
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("VALUE = 'old app'\n", encoding="utf-8")
    (tmp_path / "pipeline.py").write_text("VALUE = 'old pipe'\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("user: kept\n", encoding="utf-8")
    return tmp_path


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    monkeypatch.setattr(updater, "UPDATES_DIR", tmp_path / "cache" / "updates")
    # force the full-zipball path (no delta/network) and mark an update available
    monkeypatch.setattr(updater, "_changed_files", lambda cur, latest: None)
    monkeypatch.setattr(updater, "get_state", lambda: {
        "checked": True, "update_available": True, "latest": "v2.0.0",
        "current": "1.0.0", "zip_url": "http://fake/zip"})


def _stage_good(staging):
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "app.py").write_text("VALUE = 'new app'\n", encoding="utf-8")
    (staging / "pipeline.py").write_text("VALUE = 'new pipe'\n", encoding="utf-8")
    (staging / "VERSION").write_text("2.0.0\n", encoding="utf-8")
    (staging / "config.yaml").write_text("user: SHOULD_NOT_OVERWRITE\n",
                                         encoding="utf-8")


def test_full_update_applies_and_preserves_config(tmp_path, monkeypatch):
    _fake_repo(tmp_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "_stage_full",
                        lambda url, latest, staging: _stage_good(staging))

    msg = updater.apply_update()

    assert "Updated to v2.0.0" in msg
    assert (tmp_path / "app.py").read_text() == "VALUE = 'new app'\n"
    assert (tmp_path / "VERSION").read_text().strip() == "2.0.0"
    # local config.yaml preserved; incoming version parked for manual review
    assert (tmp_path / "config.yaml").read_text() == "user: kept\n"
    assert (tmp_path / "config.yaml.new").exists()


def test_broken_staged_update_is_rejected(tmp_path, monkeypatch):
    _fake_repo(tmp_path)
    _redirect(monkeypatch, tmp_path)

    def stage_broken(url, latest, staging):
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "app.py").write_text("def (:\n", encoding="utf-8")  # won't compile
        (staging / "pipeline.py").write_text("ok=1\n", encoding="utf-8")
        (staging / "VERSION").write_text("2.0.0\n", encoding="utf-8")

    monkeypatch.setattr(updater, "_stage_full", stage_broken)

    try:
        updater.apply_update()
        assert False, "expected the broken update to be rejected"
    except Exception:
        pass
    # nothing changed — verify runs before any file is touched
    assert (tmp_path / "app.py").read_text() == "VALUE = 'old app'\n"
    assert (tmp_path / "VERSION").read_text().strip() == "1.0.0"


def test_midapply_failure_rolls_back(tmp_path, monkeypatch):
    _fake_repo(tmp_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "_stage_full",
                        lambda url, latest, staging: _stage_good(staging))

    # fail while copying pipeline.py INTO the repo (not the backup copy) so the
    # rollback path runs after app.py was already overwritten
    real_copy2 = updater.shutil.copy2

    def flaky_copy2(src, dst, *a, **k):
        # only the apply-phase copy (from staging) fails; the rollback copy
        # (from the backup dir) must still succeed to restore the old files
        if str(dst) == str(tmp_path / "pipeline.py") and "staging" in str(src):
            raise OSError("disk full (simulated)")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(updater.shutil, "copy2", flaky_copy2)

    try:
        updater.apply_update()
        assert False, "expected mid-apply failure"
    except RuntimeError as e:
        assert "rolled back" in str(e)
    # app.py was overwritten then restored from backup; VERSION never advanced
    assert (tmp_path / "app.py").read_text() == "VALUE = 'old app'\n"
    assert (tmp_path / "VERSION").read_text().strip() == "1.0.0"


def test_check_for_update_returns_wellformed_state(monkeypatch):
    # offline-safe: stub the network so this never depends on GitHub
    monkeypatch.setattr(updater, "_latest_release",
                        lambda: {"version": "v9.9.9", "notes": "x",
                                 "zip_url": "http://fake"})
    st = updater.check_for_update()
    assert st["checked"] is True and st["update_available"] is True
    assert st["latest"] == "v9.9.9"
