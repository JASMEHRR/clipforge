"""Final audio mixdown: chain order (voice → SFX → ducked music → loudnorm),
SFX cue gating/spacing, and the royalty-free library guard. Pure — no ffmpeg."""
import copy

import music
import style_refiner
from config import load_config as _load, ROOT


def load_config() -> dict:
    # deep-copy: config.load_config returns a cached singleton; mutating it
    # would leak sfx/music overrides into other test modules
    return copy.deepcopy(_load())


def _cfg(**over) -> dict:
    cfg = load_config()
    cfg["sfx"] = {**cfg.get("sfx", {}), "enabled": True, **over}
    return cfg


# --- _mix_graph order ---------------------------------------------------------

def test_graph_order_voice_sfx_music_loudnorm():
    cfg = _cfg()
    sfx = [{"t": 1.0, "path": "x.wav"}, {"t": 3.5, "path": "y.wav"}]
    graph, n_inputs = music._mix_graph(
        30.0, cfg, sfx=sfx, has_music=True, volume_db=-18.0,
        loudnorm=music.DEFAULT_LOUDNORM)
    assert n_inputs == 3  # 2 sfx + music
    i_sfx = graph.index("adelay=1000")
    i_duck = graph.index("sidechaincompress")
    i_norm = graph.index("loudnorm")
    assert i_sfx < i_duck < i_norm
    assert "loudnorm=I=-14:TP=-1:LRA=11" in graph
    assert "adelay=3500:all=1" in graph
    # music is the LAST input, after the sfx files
    assert "[3:a]atrim" in graph


def test_graph_music_only_matches_duck_config():
    cfg = load_config()
    cfg.setdefault("music", {})["duck"] = {"threshold": 0.02, "ratio": 8}
    graph, n = music._mix_graph(10.0, cfg, sfx=[], has_music=True,
                                volume_db=-12.0,
                                loudnorm={**music.DEFAULT_LOUDNORM,
                                          "enabled": False})
    assert n == 1
    assert "sidechaincompress=threshold=0.02:ratio=8:attack=15:release=200" in graph
    assert "volume=-12.0dB" in graph
    assert "loudnorm" not in graph
    assert graph.endswith("[aout]")


def test_graph_loudnorm_only():
    graph, n = music._mix_graph(10.0, load_config(), sfx=[], has_music=False,
                                volume_db=-18.0,
                                loudnorm=music.DEFAULT_LOUDNORM)
    assert n == 0
    assert graph.startswith("[0:a]loudnorm")
    assert "amix" not in graph


def test_mix_audio_returns_none_when_nothing_to_do(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")  # existence check only; ffmpeg never runs
    cfg = load_config()
    cfg.setdefault("music", {})["loudnorm"] = {"enabled": False}
    cfg["sfx"] = {"enabled": False}
    assert music.mix_audio(clip, tmp_path / "out.mp4", cfg) is None


# --- SFX cue resolution ---------------------------------------------------------

def test_resolve_sfx_disabled_returns_empty():
    cfg = load_config()
    cfg["sfx"] = {"enabled": False}
    assert music.resolve_sfx_cues([{"t": 1.0, "kind": "pop"}], cfg) == []


def test_resolve_sfx_caps_and_sorts(monkeypatch):
    cfg = _cfg(max_per_clip=2)
    monkeypatch.setattr(music, "ensure_sfx", lambda pack, kind: f"{kind}.wav")
    cues = [{"t": 5.0, "kind": "pop"}, {"t": 1.0, "kind": "whoosh"},
            {"t": 9.0, "kind": "ding"}]
    out = music.resolve_sfx_cues(cues, cfg)
    assert [e["t"] for e in out] == [1.0, 5.0]


def test_resolve_sfx_skips_unresolvable(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(music, "ensure_sfx", lambda pack, kind: None)
    assert music.resolve_sfx_cues([{"t": 1.0, "kind": "pop"}], cfg) == []


# --- cue emission (style_refiner.sfx_cues_for) ----------------------------------

def test_cues_disabled_by_default():
    assert style_refiner.sfx_cues_for([], [], load_config()) == []


def test_cues_whoosh_at_joins_in_output_time():
    cfg = _cfg(triggers=["cuts"])
    cues = style_refiner.sfx_cues_for([4.0, 7.0], [], cfg)
    assert cues == [{"t": 4.0, "kind": "whoosh"}, {"t": 7.0, "kind": "whoosh"}]


def test_cues_emphasis_words():
    cfg = _cfg(triggers=["emphasis"])
    words = [{"word": "hello", "start": 0.5, "end": 0.9},
             {"word": "WOW", "start": 3.0, "end": 3.4},
             {"word": "really?", "start": 6.0, "end": 6.5},
             {"word": "I", "start": 8.0, "end": 8.1}]  # short caps: no cue
    cues = style_refiner.sfx_cues_for([], words, cfg)
    assert cues == [{"t": 3.0, "kind": "pop"}, {"t": 6.0, "kind": "pop"}]


def test_cues_spacing_and_cap():
    cfg = _cfg(triggers=["emphasis"], max_per_clip=2)
    words = [{"word": f"WORD{i}!", "start": float(i), "end": i + 0.4}
             for i in range(6)]  # 1 s apart — closer than the 1.5 s floor
    cues = style_refiner.sfx_cues_for([], words, cfg)
    assert len(cues) == 2
    assert cues[1]["t"] - cues[0]["t"] >= 1.5


# --- library guard ---------------------------------------------------------------

def test_library_guard_accepts_library_track():
    p = ROOT / "assets" / "music" / "track.mp3"
    assert music.check_library(p, load_config()) is None


def test_library_guard_warns_outside(tmp_path):
    warn = music.check_library(tmp_path / "sketchy.mp3", load_config())
    assert warn and "outside the royalty-free library" in warn
