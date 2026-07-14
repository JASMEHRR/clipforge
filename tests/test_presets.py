"""Editing presets: schema gate, CRUD roundtrip, expand() mapping into
apply_run_options keys, and the config effects of those keys."""
import copy

import pytest

import presets
from config import apply_run_options, load_config as _load


def load_config() -> dict:
    return copy.deepcopy(_load())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(presets, "PRESETS_DIR", tmp_path / "presets")


FULL = {
    "name": "Gaming Punchy",
    "caption": {"preset": "karaoke-pop", "font_family": "Montserrat ExtraBold",
                "highlight_hex": "#39FF14", "animation": "karaoke",
                "font_size": 72},
    "aspect": "9:16",
    "cta_text": "Follow for more!",
    "sfx": {"enabled": True, "pack": "default", "volume_db": -8},
    "music": {"track": "auto", "volume_db": -16},
    "speed_ramps": {"enabled": True, "rate": 1.5},
    "punch_in": {"mode": "emphasis", "amount_pct": 7},
    "popins": [{"keyword": "fire", "asset": "assets/popins/fire.png"}],
    "transition": "whip",
    "watermark": {"mode": "text", "text": "clipped by @me",
                  "position": "bottom-right"},
}


def test_crud_roundtrip():
    presets.save_preset(FULL)
    assert presets.load_preset("Gaming Punchy")["cta_text"] == "Follow for more!"
    assert [p["name"] for p in presets.list_presets()] == ["Gaming Punchy"]
    presets.delete_preset("gaming-punchy")  # slug or name both address it
    assert presets.list_presets() == []


def test_schema_rejects_bad_preset():
    with pytest.raises(presets.PresetError):
        presets.save_preset({"name": "x", "aspect": "4:3"})
    with pytest.raises(presets.PresetError):
        presets.save_preset({"caption": {}})  # missing name


def test_list_skips_malformed_file(tmp_path):
    presets.save_preset(FULL)
    (presets.PRESETS_DIR / "broken.json").write_text("{nope", encoding="utf-8")
    assert [p["name"] for p in presets.list_presets()] == ["Gaming Punchy"]


def test_expand_maps_all_keys():
    opts = presets.expand(FULL)
    assert opts["preset"] == "karaoke-pop"
    assert opts["font_family"] == "Montserrat ExtraBold"
    assert opts["highlight_hex"] == "#39FF14"
    assert opts["caption_animation"] == "karaoke"
    assert opts["caption_font_size"] == 72
    assert opts["cta_text"] == "Follow for more!"
    assert opts["sfx_enabled"] is True and opts["sfx_volume_db"] == -8
    assert opts["speed_ramps"] == {"enabled": True, "rate": 1.5}
    assert opts["punch_in"]["mode"] == "emphasis"
    assert opts["popins"][0]["keyword"] == "fire"
    assert opts["transition"] == "whip"
    assert opts["watermark_mode"] == "text"
    assert opts["watermark_text"] == "clipped by @me"
    # run-level keys for the caller
    assert opts["aspect"] == "9:16"
    assert opts["music"] == "auto" and opts["music_volume_db"] == -16


def test_expand_empty_preset_is_noop():
    assert presets.expand({"name": "bare"}) == {}


def test_apply_run_options_preset_keys():
    cfg = apply_run_options(load_config(), presets.expand(FULL))
    cap = cfg["captions"]["presets"]["karaoke-pop"]
    assert cap["style"] == "karaoke" and cap["font_size"] == 72
    assert cfg["sfx"]["enabled"] is True and cfg["sfx"]["volume_db"] == -8.0
    assert cfg["style"]["speed_ramps"] == {"enabled": True, "rate": 1.5}
    assert cfg["style"]["punch_in"]["amount_pct"] == 7
    assert cfg["style"]["transition"] == "whip"
    assert cfg["captions"]["watermark"]["mode"] == "text"


def test_apply_run_options_empty_still_noop():
    base = load_config()
    assert apply_run_options(base, {}) == base


def test_api_crud_roundtrip():
    from fastapi.testclient import TestClient
    from server import create_app
    client = TestClient(create_app())
    r = client.post("/api/edit-presets", json={"preset": FULL})
    assert r.status_code == 200
    names = [p["name"] for p in client.get("/api/edit-presets").json()["presets"]]
    assert "Gaming Punchy" in names
    assert client.post("/api/edit-presets",
                       json={"preset": {"name": "x", "aspect": "4:3"}}
                       ).status_code == 422
    assert client.delete("/api/edit-presets/Gaming%20Punchy").status_code == 200
    assert client.delete("/api/edit-presets/Gaming%20Punchy").status_code == 404
