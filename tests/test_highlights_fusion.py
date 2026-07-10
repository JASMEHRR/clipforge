"""viral_v2 module 2: event fusion into highlight selection. All keyless."""
import json

import highlights as hl


def _evt(t0, t1, etype="laughter", intensity=8.0):
    return {"type": etype, "t_start_s": float(t0), "t_end_s": float(t1),
            "description": "d", "intensity_1_10": float(intensity),
            "actors_hint": "", "source": "audio"}


def _cand(start, end, score=6.0):
    return {"start": float(start), "end": float(end), "hook": "h",
            "reason": "r", "score": float(score)}


def _transcript(text="", sentences=None):
    return {"text": text, "language": "en", "duration": 600.0,
            "words": [], "sentences": sentences or []}


# --------------------------------------------------------------- fusion

def test_fuse_is_additive_and_clamped(cfg):
    cands = [_cand(10, 50, score=6.0), _cand(100, 140, score=9.9)]
    events = [_evt(20, 25), _evt(110, 112, intensity=10.0)]
    fused = hl.fuse_event_scores(cands, events, cfg)
    assert fused[0]["score"] > 6.0
    assert fused[1]["score"] <= 10.0
    # bounds untouched by scoring
    assert (fused[0]["start"], fused[0]["end"]) == (10.0, 50.0)


def test_fuse_no_events_in_span_is_identity(cfg):
    cands = [_cand(10, 50)]
    fused = hl.fuse_event_scores(cands, [_evt(200, 205)], cfg)
    assert fused == cands


def test_fuse_empty_events_is_identity(cfg):
    cands = [_cand(10, 50)]
    assert hl.fuse_event_scores(cands, [], cfg) == cands


# ------------------------------------------------------- boundary rules

def test_end_extends_into_reaction(cfg):
    # reaction starts 3s after clip end (within reaction_window_s=6)
    c = _cand(10, 45)
    out = hl.apply_reaction_boundaries([c], [_evt(48, 52)], cfg, 600.0)
    assert out[0]["end"] == 53.0          # event end + 1s tail
    assert out[0]["start"] == 10.0


def test_end_extension_respects_max_seconds(cfg):
    max_s = cfg["clips"]["max_seconds"]     # 60
    c = _cand(10, 65)                       # near the cap already
    out = hl.apply_reaction_boundaries([c], [_evt(68, 90)], cfg, 600.0)
    assert out[0]["end"] == 10.0 + max_s


def test_end_extension_respects_duration(cfg):
    c = _cand(10, 45)
    out = hl.apply_reaction_boundaries([c], [_evt(48, 52)], cfg, 50.0)
    assert out[0]["end"] == 50.0


def test_reaction_outside_window_is_ignored(cfg):
    c = _cand(10, 45)
    out = hl.apply_reaction_boundaries([c], [_evt(60, 62)], cfg, 600.0)
    assert out[0]["end"] == 45.0


def test_non_reaction_types_do_not_extend(cfg):
    c = _cand(10, 45)
    out = hl.apply_reaction_boundaries(
        [c], [_evt(48, 52, etype="energy_spike")], cfg, 600.0)
    assert out[0]["end"] == 45.0


def test_start_never_begins_mid_event(cfg):
    c = _cand(20, 55)                       # starts inside the 18-23 event
    out = hl.apply_reaction_boundaries([c], [_evt(18, 23)], cfg, 600.0)
    assert out[0]["start"] == 17.5          # event start - 0.5s lead
    assert out[0]["end"] == 55.0


def test_start_pull_clamped_at_zero(cfg):
    c = _cand(0.2, 40)
    out = hl.apply_reaction_boundaries([c], [_evt(0.0, 3.0)], cfg, 600.0)
    assert out[0]["start"] == 0.0


# ------------------------------------------------ event-cluster candidates

def test_event_cluster_candidates_from_silent_source(cfg):
    events = [_evt(1000, 1003, etype="physical_event", intensity=9.0),
              _evt(2000, 2002, etype="laughter", intensity=6.0)]
    cands = hl.event_cluster_candidates(events, 21600.0, cfg)
    assert len(cands) == 2
    min_s, max_s = cfg["clips"]["min_seconds"], cfg["clips"]["max_seconds"]
    for c in cands:
        assert min_s <= c["end"] - c["start"] <= max_s
    # the fall is covered by the top-scored candidate, with lead-in before it
    top = cands[0]
    assert top["start"] <= 1000.0 <= top["end"]
    assert top["score"] > cands[1]["score"]


def test_event_cluster_candidates_dedupe_overlaps(cfg):
    events = [_evt(100, 102, intensity=9.0), _evt(103, 105, intensity=8.0)]
    cands = hl.event_cluster_candidates(events, 600.0, cfg)
    assert len(cands) == 1


def test_is_sparse(cfg):
    dense = _transcript(text=" ".join(["word"] * 2000))  # 200 wpm over 10 min
    sparse = _transcript(text="only a few words here")
    assert hl._is_sparse(dense, 600.0, cfg) is False
    assert hl._is_sparse(sparse, 600.0, cfg) is True


# ---------------------------------------------- select_highlights identity

def _sentences(duration=300.0, sent_s=10.0):
    out, t = [], 0.0
    i = 0
    while t + sent_s <= duration:
        words = [{"word": f"w{i}_{k}", "start": round(t + k, 3),
                  "end": round(t + k + 0.8, 3)} for k in range(int(sent_s))]
        out.append({"text": f"Sentence number {i} with several words spoken.",
                    "start": round(t, 3), "end": round(t + sent_s, 3),
                    "words": words})
        t += sent_s
        i += 1
    return out


def test_select_highlights_events_none_is_identical(cfg):
    sents = _sentences()
    tr = _transcript(text=" ".join(s["text"] for s in sents), sentences=sents)
    scenes = {"scenes": [{"index": 0, "start": 0.0, "end": 300.0}]}
    a = hl.select_highlights(tr, scenes, 300.0, cfg, provider="mock")
    b = hl.select_highlights(tr, scenes, 300.0, cfg, provider="mock",
                             events=None)
    c = hl.select_highlights(tr, scenes, 300.0, cfg, provider="mock",
                             events=[])
    assert json.dumps(a) == json.dumps(b) == json.dumps(c)


def test_select_highlights_with_events_still_schema_valid(cfg):
    sents = _sentences()
    tr = _transcript(text=" ".join(s["text"] for s in sents), sentences=sents)
    scenes = {"scenes": [{"index": 0, "start": 0.0, "end": 300.0}]}
    events = [_evt(35, 38, intensity=9.0), _evt(120, 123)]
    out = hl.select_highlights(tr, scenes, 300.0, cfg, provider="mock",
                               events=events)
    from schemas import validate
    validate({"candidates": out}, "highlight_candidates")
    assert out


def test_select_highlights_sparse_gets_event_clusters(cfg):
    tr = _transcript(text="")           # no transcript at all -> mechanical
    scenes = {"scenes": [{"index": 0, "start": 0.0, "end": 7200.0}]}
    events = [_evt(3600, 3603, etype="physical_event", intensity=9.5)]
    out = hl.select_highlights(tr, scenes, 7200.0, cfg, provider="mock",
                               events=events)
    assert any(c["start"] <= 3600.0 <= c["end"] for c in out), \
        "the fall at 1:00:00 must be covered by a candidate"
