"""viral_v2 module 1: DSP events, merging, quota, privacy gate, provider order.
All keyless — no network, no ffmpeg."""
import json
import wave
from datetime import date

import numpy as np
import pytest

import video_events as ve
from schemas import validate


def _evt(t0, t1, etype="laughter", intensity=5.0, source="audio", desc="d",
         actors=""):
    return {"type": etype, "t_start_s": float(t0), "t_end_s": float(t1),
            "description": desc, "intensity_1_10": float(intensity),
            "actors_hint": actors, "source": source}


# ------------------------------------------------------------- mmss parsing

def test_mmss_to_s():
    assert ve._mmss_to_s("0:00") == 0.0
    assert ve._mmss_to_s("1:23") == 83.0
    assert ve._mmss_to_s("12:03") == 723.0
    assert ve._mmss_to_s("1:02:03") == 3723.0
    assert ve._mmss_to_s("garbage") == 0.0


def test_to_absolute_clamps_to_chunk():
    raw = [{"type": "laughter", "t_start": "9:00", "t_end": "99:00",
            "description": "d", "intensity_1_10": 7.0}]
    out = ve._to_absolute(raw, chunk_start=600.0, chunk_seconds=600.0,
                          source="gemini")
    assert out[0]["t_start_s"] == 1140.0          # 600 + 540
    assert out[0]["t_end_s"] == 1200.0            # clamped to chunk end
    assert out[0]["source"] == "gemini"
    assert out[0]["actors_hint"] == ""


# ----------------------------------------------------------------- merging

def test_merge_overlapping_takes_max_intensity_and_union():
    a = _evt(10, 12, intensity=5.0)
    b = _evt(11, 15, intensity=8.0, source="gemini", desc="stronger")
    m = ve.merge_events([a], [b])
    assert len(m) == 1
    assert m[0]["t_start_s"] == 10.0 and m[0]["t_end_s"] == 15.0
    assert m[0]["intensity_1_10"] == 8.0
    assert m[0]["description"] == "stronger" and m[0]["source"] == "gemini"


def test_merge_within_2s_gap():
    m = ve.merge_events([_evt(10, 12), _evt(13.5, 16)])
    assert len(m) == 1 and m[0]["t_end_s"] == 16.0


def test_merge_keeps_disjoint_events():
    m = ve.merge_events([_evt(10, 12), _evt(20, 22)])
    assert len(m) == 2


def test_merge_inputs_not_mutated():
    a = _evt(10, 12)
    ve.merge_events([a], [_evt(11, 15, intensity=9.0)])
    assert a["t_end_s"] == 12.0


# --------------------------------------------------------------- audio DSP

def _write_wav(path, data, sr=16000):
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def test_audio_events_synthetic_wav(tmp_path, cfg):
    sr = 16000
    t = np.arange(30 * sr) / sr
    quiet = 0.02 * np.sin(2 * np.pi * 220 * t)      # low-level speech-ish bed
    sig = quiet.copy()
    # energy spike: loud pure tone at 10-12s (tonal -> low flatness/zcr)
    tone = (np.arange(10 * sr, 12 * sr) / sr)
    sig[10 * sr:12 * sr] = 0.8 * np.sin(2 * np.pi * 440 * tone)
    # laughter-like burst: loud amplitude-modulated white noise at 20-22s
    rng = np.random.default_rng(42)
    n = 2 * sr
    am = 0.5 * (1 + np.sin(2 * np.pi * 5 * np.arange(n) / sr))  # ~5 Hz syllables
    sig[20 * sr:22 * sr] = 0.6 * am * rng.standard_normal(n)
    wav = tmp_path / "a.wav"
    _write_wav(wav, sig, sr)

    events = ve.audio_events(wav, cfg)
    validate({"events": events}, "event_timeline")
    spikes = [e for e in events if e["type"] == "energy_spike"]
    laughs = [e for e in events if e["type"] == "laughter"]
    assert any(9.0 <= e["t_start_s"] <= 11.0 for e in spikes), spikes
    assert any(19.0 <= e["t_start_s"] <= 21.0 for e in laughs), laughs
    assert all(e["source"] == "audio" for e in events)


def test_audio_events_silence_yields_nothing(tmp_path, cfg):
    wav = tmp_path / "s.wav"
    _write_wav(wav, np.zeros(16000 * 10))
    assert ve.audio_events(wav, cfg) == []


def test_audio_events_unreadable_returns_empty(tmp_path, cfg):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    assert ve.audio_events(bad, cfg) == []


# ------------------------------------------------------------------- quota

def _cfg_with_cache(cfg, tmp_path, **viral):
    c = json.loads(json.dumps(cfg))  # deep copy, cfg fixture stays pristine
    c["paths"]["cache_dir"] = str(tmp_path / "cache")
    c["viral_v2"].update(viral)
    return c


def test_quota_accumulates_and_limits(tmp_path, cfg):
    c = _cfg_with_cache(cfg, tmp_path, max_daily_minutes=10)
    assert ve.quota_remaining_minutes(c) == 10.0
    ve._quota_add(c, 6.0)
    assert ve.quota_remaining_minutes(c) == 4.0
    ve._quota_add(c, 6.0)
    assert ve.quota_remaining_minutes(c) == 0.0


def test_quota_is_per_day(tmp_path, cfg):
    c = _cfg_with_cache(cfg, tmp_path, max_daily_minutes=10)
    usage = {"2001-01-01": 999.0}  # yesterday's burn doesn't count
    p = ve._usage_path(c)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(usage))
    assert ve.quota_remaining_minutes(c) == 10.0
    ve._quota_add(c, 1.0)
    saved = json.loads(p.read_text())
    assert saved["2001-01-01"] == 999.0
    assert saved[date.today().isoformat()] == 1.0


def test_chunk_cache_roundtrip(tmp_path, cfg):
    c = _cfg_with_cache(cfg, tmp_path)
    events = [_evt(1, 2, source="gemini")]
    ve._cache_put(c, "abc_def_gemini", events)
    assert ve._cache_get(c, "abc_def_gemini") == events
    assert ve._cache_get(c, "missing") is None


# ------------------------------------------- privacy gate + provider order

def test_privacy_gate_blocks_cloud_for_local_file(tmp_path, cfg, monkeypatch):
    c = _cfg_with_cache(cfg, tmp_path, allow_upload=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    called = {"cloud": False}
    monkeypatch.setattr(ve, "_cloud_events",
                        lambda *a, **k: called.update(cloud=True) or [])
    monkeypatch.setattr(ve, "audio_events", lambda *a, **k: [])
    out = ve.detect_events("v.mp4", "a.wav", 60.0,
                           {"source_type": "file"}, c, provider="gemini")
    assert called["cloud"] is False and out == []


def test_url_source_is_exempt_from_privacy_gate(tmp_path, cfg, monkeypatch):
    c = _cfg_with_cache(cfg, tmp_path, allow_upload=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    called = {"cloud": False}
    monkeypatch.setattr(ve, "_cloud_events",
                        lambda *a, **k: called.update(cloud=True) or [])
    monkeypatch.setattr(ve, "audio_events", lambda *a, **k: [])
    ve.detect_events("v.mp4", "a.wav", 60.0, {"source_type": "url"}, c,
                     provider="gemini")
    assert called["cloud"] is True


def test_allow_upload_true_enables_cloud_for_local_file(tmp_path, cfg,
                                                        monkeypatch):
    c = _cfg_with_cache(cfg, tmp_path, allow_upload=True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    called = {"cloud": False}
    monkeypatch.setattr(ve, "_cloud_events",
                        lambda *a, **k: called.update(cloud=True) or [])
    monkeypatch.setattr(ve, "audio_events", lambda *a, **k: [])
    ve.detect_events("v.mp4", "a.wav", 60.0, {"source_type": "file"}, c,
                     provider="gemini")
    assert called["cloud"] is True


def test_provider_order_falls_to_openrouter_on_quota(tmp_path, cfg,
                                                     monkeypatch):
    c = _cfg_with_cache(cfg, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    chunks = [{"path": tmp_path / "c0.mp4", "start_s": 0.0, "seconds": 60.0,
               "sha": "s0"},
              {"path": tmp_path / "c1.mp4", "start_s": 60.0, "seconds": 60.0,
               "sha": "s1"}]
    monkeypatch.setattr(ve, "chunk_video", lambda *a, **k: chunks)

    def gem(chunk, cfg_, prompt):
        raise ve.QuotaExhausted("RPD hit")

    seen = []

    def orouter(chunk, cfg_, prompt):
        seen.append(chunk["sha"])
        return [_evt(chunk["start_s"] + 1, chunk["start_s"] + 2,
                     source="openrouter")]

    monkeypatch.setattr(ve, "gemini_chunk_events", gem)
    monkeypatch.setattr(ve, "openrouter_chunk_events", orouter)
    events = ve._cloud_events("v.mp4", 120.0, c, None)
    assert seen == ["s0", "s1"]
    assert all(e["source"] == "openrouter" for e in events)


def test_keyless_detect_events_uses_mock_canned_events(tmp_path, cfg,
                                                       monkeypatch):
    c = _cfg_with_cache(cfg, tmp_path)
    monkeypatch.setattr(ve, "audio_events", lambda *a, **k: [])
    events = ve.detect_events("v.mp4", "a.wav", 120.0,
                              {"source_type": "file"}, c, provider="mock")
    validate({"events": events}, "event_timeline")
    assert events and all(e["source"] == "mock" for e in events)
    assert all(e["t_end_s"] <= 120.0 for e in events)


def test_available_cloud_providers_respects_keys(tmp_path, cfg, monkeypatch):
    c = _cfg_with_cache(cfg, tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    assert ve._available_cloud_providers(c) == ["openrouter"]
