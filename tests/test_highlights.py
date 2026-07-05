import math

import highlights as hl


def _sents(spans):
    return [{"text": f"Sentence {i}.", "start": s, "end": e,
             "words": [{"word": f"w{i}.", "start": s, "end": e}]}
            for i, (s, e) in enumerate(spans)]


def _transcript(spans):
    sents = _sents(spans)
    words = [w for s in sents for w in s["words"]]
    return {"text": " ".join(s["text"] for s in sents), "language": "en",
            "duration": spans[-1][1] if spans else 0.0,
            "words": words, "sentences": sents}


SPANS = [(i * 10.0, i * 10.0 + 9.5) for i in range(30)]  # 10s sentences, 300s


def test_snap_within_bounds():
    r = hl.snap_to_sentences(11.0, 52.0, _sents(SPANS), 30, 60)
    assert r is not None
    s, e = r
    assert 30 <= e - s <= 60
    assert s in {sp[0] for sp in SPANS} and e in {sp[1] for sp in SPANS}


def test_snap_expands_short_range():
    r = hl.snap_to_sentences(10.0, 15.0, _sents(SPANS), 30, 60)
    assert r is not None and 30 <= r[1] - r[0] <= 60


def test_snap_impossible_returns_none():
    # single 5s sentence — can never make a 30s clip
    r = hl.snap_to_sentences(0.0, 5.0, _sents([(0.0, 5.0)]), 30, 60)
    assert r is None


def test_build_windows_durations(cfg):
    t = _transcript(SPANS)
    ws = hl.build_windows(t, cfg)
    assert ws
    for w in ws:
        assert (cfg["clips"]["min_seconds"] <= w["end"] - w["start"]
                <= cfg["clips"]["max_seconds"])


def test_windows_capped(cfg):
    t = _transcript([(i * 10.0, i * 10.0 + 9.5) for i in range(200)])
    assert len(hl.build_windows(t, cfg, max_windows=24)) <= 24


def test_mechanical_windows(cfg):
    ws = hl.mechanical_windows(300.0, cfg)
    assert len(ws) >= 3
    for w in ws:
        assert w["end"] - w["start"] >= cfg["clips"]["min_seconds"] - 0.01


def test_dedupe_removes_overlaps():
    cands = [
        {"start": 0, "end": 40, "score": 9, "hook": "a", "reason": ""},
        {"start": 5, "end": 45, "score": 8, "hook": "b", "reason": ""},   # overlaps
        {"start": 100, "end": 140, "score": 7, "hook": "c", "reason": ""},
    ]
    kept = hl._dedupe(cands, 10)
    assert len(kept) == 2 and kept[0]["score"] == 9


def test_rule_based_scores_in_range(cfg):
    ws = [{"start": 0, "end": 40,
           "text": "Here is the secret mistake everyone makes. Why? Nobody knows."},
          {"start": 50, "end": 90, "text": "uh um so yeah"}]
    cands = hl.rule_based_candidates(ws, cfg)
    assert all(0 <= c["score"] <= 10 for c in cands)
    assert cands[0]["score"] > cands[1]["score"]  # keyword-rich beats filler


def test_select_highlights_mock_end_to_end(cfg):
    t = _transcript(SPANS)
    scenes = {"scenes": [{"index": 0, "start": 0.0, "end": 300.0}]}
    cands = hl.select_highlights(t, scenes, 300.0, cfg, provider="mock")
    assert cands
    sb = {sp[0] for sp in SPANS}
    se = {sp[1] for sp in SPANS}
    for c in cands:
        assert 30 <= c["end"] - c["start"] <= 60
        assert c["start"] in sb and c["end"] in se


def test_min_keep_rule(cfg):
    t = _transcript(SPANS)
    clips = [{"index": i, "start": i * 60.0, "end": i * 60.0 + 40}
             for i in range(6)]
    out = hl.rescore_clips(clips, t, cfg, provider="mock")
    kept = [c for c in out if c["kept"]]
    expected = max(cfg["clips"]["min_keep"],
                   math.ceil(6 * cfg["clips"]["keep_ratio"]))
    assert len(kept) == expected


def test_min_keep_rule_small_set(cfg):
    t = _transcript(SPANS)
    clips = [{"index": 0, "start": 0.0, "end": 40.0},
             {"index": 1, "start": 60.0, "end": 100.0}]
    out = hl.rescore_clips(clips, t, cfg, provider="mock")
    assert all(c["kept"] for c in out)  # <= min_keep → keep all
