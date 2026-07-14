"""Approved channels & auto-pull: the permission gate, pool dedupe, top/new
merge, sequential claim priority, and credit-text threading. yt-dlp is mocked
via the _fetch_entries seam — fully offline."""
import copy

import pytest

import channels
from config import apply_run_options, load_config as _load


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(channels, "STORE_PATH", tmp_path / "channels.json")


def _cfg() -> dict:
    return copy.deepcopy(_load())


def _add(url="https://youtube.com/@creator", permission="Whop program",
         credit="clips: @creator", **kw):
    return channels.add_channel(url, permission, credit, **kw)


def _entries(n, views=True):
    return [{"id": f"vid{i}", "title": f"Video {i}",
             "url": f"https://youtube.com/watch?v=vid{i}",
             "view_count": (n - i) * 1000 if views else None}
            for i in range(n)]


# --- permission gate -------------------------------------------------------

def test_add_requires_permission_and_credit():
    with pytest.raises(channels.ChannelError):
        _add(permission="")
    with pytest.raises(channels.ChannelError):
        _add(credit="  ")
    ch = _add()
    assert ch["permission_source"] == "Whop program"


def test_gate_blocks_poll(monkeypatch):
    ch = _add()
    channels.update_channel(ch["id"], {"paused": True})
    called = []
    monkeypatch.setattr(channels, "_fetch_entries",
                        lambda url, limit: called.append(url) or [])
    channels.poll_all(_cfg())
    assert called == []                       # paused → never fetched
    channels.update_channel(ch["id"], {"paused": False})
    channels.poll_all(_cfg())
    assert len(called) == 1


def test_permission_cannot_be_blanked():
    ch = _add()
    with pytest.raises(channels.ChannelError):
        channels.update_channel(ch["id"], {"permission_source": ""})
    with pytest.raises(channels.ChannelError):
        channels.update_channel(ch["id"], {"credit_text": " "})


def test_duplicate_url_rejected():
    _add()
    with pytest.raises(channels.ChannelError):
        _add()


# --- poll: top-N, new uploads, dedupe ----------------------------------------

def test_first_poll_pulls_top_n_only(monkeypatch):
    ch = _add(top_n=3)
    monkeypatch.setattr(channels, "_fetch_entries",
                        lambda url, limit: _entries(20))
    r = channels.poll_all(_cfg())
    assert r["added"] == 3                    # backfill = top-N, not history
    pool = channels.load_store()["pool"]
    assert set(pool) == {"vid0", "vid1", "vid2"}  # highest view counts
    assert all(e["source"] == "top" for e in pool.values())


def test_second_poll_adds_new_uploads(monkeypatch):
    ch = _add(top_n=3)
    monkeypatch.setattr(channels, "_fetch_entries",
                        lambda url, limit: _entries(20))
    channels.poll_all(_cfg())
    # a fresh upload appears at the head of the /videos tab with few views
    fresh = [{"id": "brandnew", "title": "New!", "url": "u",
              "view_count": 5}] + _entries(20)
    monkeypatch.setattr(channels, "_fetch_entries", lambda url, limit: fresh)
    r = channels.poll_all(_cfg())
    assert r["added"] == 1
    assert channels.load_store()["pool"]["brandnew"]["source"] == "new"


def test_never_processed_twice(monkeypatch):
    _add(top_n=2)
    monkeypatch.setattr(channels, "_fetch_entries",
                        lambda url, limit: _entries(5))
    channels.poll_all(_cfg())
    channels.poll_all(_cfg())
    assert len(channels.load_store()["pool"]) == 2


def test_poll_error_isolated(monkeypatch):
    _add(url="https://youtube.com/@a")
    _add(url="https://youtube.com/@b")

    def fetch(url, limit):
        if "@a" in url:
            raise channels.ChannelError("boom")
        return _entries(3)

    monkeypatch.setattr(channels, "_fetch_entries", fetch)
    r = channels.poll_all(_cfg())
    assert r["added"] == 3 and len(r["errors"]) == 1


def test_missing_view_counts_fall_back_to_order(monkeypatch):
    _add(top_n=2)
    monkeypatch.setattr(channels, "_fetch_entries",
                        lambda url, limit: _entries(6, views=False))
    channels.poll_all(_cfg())
    assert set(channels.load_store()["pool"]) == {"vid0", "vid1"}


# --- processing priority + credit text ---------------------------------------

def test_claim_prefers_new_over_top(monkeypatch):
    ch = _add(top_n=1)
    store = channels.load_store()
    store["pool"] = {
        "old_top": {"channel_id": ch["id"], "title": "t", "url": "u",
                    "views": 9, "source": "top", "status": "new",
                    "added_at": "2026-07-14T01:00:00"},
        "fresh": {"channel_id": ch["id"], "title": "n", "url": "u",
                  "views": 1, "source": "new", "status": "new",
                  "added_at": "2026-07-14T02:00:00"},
    }
    channels.save_store(store)
    vid, entry = channels._claim_next()
    assert vid == "fresh"
    assert channels.load_store()["pool"]["fresh"]["status"] == "processing"


def test_credit_text_lands_in_metadata_config():
    cfg = apply_run_options(_cfg(), {"credit_text": "clips: @creator"})
    assert cfg["metadata"]["credit_text"] == "clips: @creator"
    # blank credit leaves config untouched
    cfg2 = apply_run_options(_cfg(), {"credit_text": "  "})
    assert "credit_text" not in cfg2.get("metadata", {})


def test_channel_stats_counts():
    ch = _add()
    store = channels.load_store()
    store["pool"] = {
        "a": {"channel_id": ch["id"], "title": "", "url": "", "views": 1,
              "source": "top", "status": "processed", "added_at": ""},
        "b": {"channel_id": ch["id"], "title": "", "url": "", "views": 1,
              "source": "new", "status": "new", "added_at": ""},
    }
    channels.save_store(store)
    (s,) = channels.channel_stats()
    assert s["videos_pulled"] == 2 and s["pending"] == 1 and s["processed"] == 1
