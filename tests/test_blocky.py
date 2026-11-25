"""Blocky mode: pure-logic checks (story cleaning, timing, sound cues) and the
schema/renderer option lists staying in step."""
import random
import re
from pathlib import Path

import pytest

import blocky
import captions
import llm
import schemas
from config import load_config
from schemas import validate


def _story(**over):
    s = llm._mock_blocky_story("In this town, every time you lie, a brick falls off.")
    s.update(over)
    return s


def _words(text, t0=0.0, step=0.3):
    return [{"word": f" {w}", "start": round(t0 + i * step, 3),
             "end": round(t0 + i * step + 0.25, 3)} for i, w in enumerate(text.split())]


def test_mock_story_is_schema_valid_and_cleans():
    raw = _story()
    validate(raw, "blocky_story")
    story = blocky.clean_story(raw)
    assert len(story["shots"]) == len(raw["shots"])
    assert story["cast"][0]["kind"] == "person"


def test_clean_story_drops_unknown_actors_and_moves_focus_to_the_front():
    raw = _story()
    raw["shots"][1]["actors"] = [
        {"id": "ghost", "action": "walk", "face": "smile", "prop": "none"},
        {"id": "milo", "action": "walk", "face": "smile", "prop": "none"},
        {"id": "gran", "action": "wave", "face": "laugh", "prop": "none"},
        {"id": "gran", "action": "wave", "face": "laugh", "prop": "none"},
    ]
    raw["shots"][1]["focus"] = "gran"
    shot = blocky.clean_story(raw)["shots"][1]
    assert [a["cast"] for a in shot["actors"]] == [1, 0]     # gran first, no ghost, no dup
    assert shot["focus"] == 0


def test_clean_story_focus_fallbacks():
    raw = _story()
    raw["shots"][0]["focus"] = "sign"                      # no sign text -> first actor
    raw["shots"][2]["focus"] = "nobody"
    raw["shots"][3]["sign"] = "  NO   YAWNING  "
    raw["shots"][3]["focus"] = "sign"
    s = blocky.clean_story(raw)["shots"]
    assert s[0]["focus"] == 0 and s[2]["focus"] == 0
    assert s[3]["focus"] == "sign" and s[3]["sign"] == "NO YAWNING"
    assert blocky.clean_story(_story())["shots"][-1]["focus"] == -1   # group of two


def test_clean_story_keeps_only_emphasis_words_that_are_spoken():
    raw = _story()
    raw["shots"][3]["emphasis"] = ["shrinks", "elephant"]
    assert blocky.clean_story(raw)["shots"][3]["emphasis"] == ["shrinks"]


def test_clean_story_maps_slips_onto_drawable_options_and_lists_them():
    raw = _story()
    raw["cast"][0].update(shirt="Light Blue", hair="bald", shape="robot")
    raw["shots"][1]["actors"][0].update(action="laugh", face="happy", prop="sword")
    raw["shots"][1].update(place="living room", camera="spiral", sfx="boing")
    validate(raw, "blocky_story")                          # loose schema accepts it
    story = blocky.clean_story(raw)
    milo, shot = story["cast"][0], story["shots"][1]
    assert (milo["shirt"], milo["hair"], milo["shape"], milo["kind"]) == \
        ("cyan", "none", "person", "person")
    a = shot["actors"][0]
    assert (a["action"], a["face"], a["prop"]) == ("celebrate", "smile", "none")
    assert (shot["place"], shot["sfx"]) == ("room", "none")
    assert shot["camera"] in schemas.BLOCKY_CAMERAS
    assert any("robot" in f for f in story["fixes"]) and any("spiral" in f for f in story["fixes"])
    assert blocky.clean_story(_story())["fixes"] == []


def test_clean_story_rejects_a_story_with_too_few_shots():
    raw = _story()
    raw["shots"] = raw["shots"][:2]
    with pytest.raises(blocky.BlockyError):
        blocky.clean_story(raw)


def test_build_spec_times_and_lose_part_pop_on_the_emphasis_word():
    raw = _story()
    raw["shots"][3]["actors"][0]["action"] = "lose_part"
    raw["shots"][3]["emphasis"] = ["shrinks"]
    story = blocky.clean_story(raw)
    n = len(story["shots"])
    bounds = [i * 3.0 for i in range(n + 1)]
    words = _words(story["shots"][3]["text"], t0=9.0)
    spec = blocky.build_spec(story, bounds, words, 1080, 1920, 30, 5, 0.9)
    assert [s["start"] for s in spec["shots"]] == bounds[:-1]
    assert spec["total"] == bounds[-1]
    k = story["shots"][3]["text"].split().index("shrinks")
    assert spec["shots"][3]["actors"][0]["lose_t"] == pytest.approx(k * 0.3)
    assert spec == blocky.build_spec(story, bounds, words, 1080, 1920, 30, 5, 0.9)


def test_build_spec_rejects_mismatched_bounds():
    story = blocky.clean_story(_story())
    with pytest.raises(blocky.BlockyError):
        blocky.build_spec(story, [0.0, 1.0], [], 1080, 1920, 30, 1, 0.9)


def test_sfx_cues_are_sorted_capped_and_riser_lands_on_the_cut():
    story = blocky.clean_story(_story())
    bounds = [i * 3.0 for i in range(len(story["shots"]) + 1)]
    spec = blocky.build_spec(story, bounds, [], 1080, 1920, 30, 5, 0.9)
    cues = blocky.sfx_cues(story, spec, [], cap=20)
    assert [c["t"] for c in cues] == sorted(c["t"] for c in cues)
    riser = [c for c in cues if c["kind"] == "riser"][0]
    k = next(i for i, s in enumerate(story["shots"]) if s["sfx"] == "riser")
    assert riser["t"] == pytest.approx(bounds[k] - 0.75)
    assert len(blocky.sfx_cues(story, spec, [], cap=2)) == 2


def test_mark_emphasis_flags_words_only_inside_their_shot():
    raw = _story()
    raw["shots"][1]["emphasis"] = ["yawned"]
    story = blocky.clean_story(raw)
    bounds = [i * 3.0 for i in range(len(story["shots"]) + 1)]
    words = _words(story["shots"][1]["text"], t0=3.0) + [{"word": " yawned", "start": 10.0,
                                                         "end": 10.2}]
    marked = blocky.mark_emphasis(story, bounds, words)
    flagged = [w["word"].strip() for w in marked if w.get("emph")]
    assert flagged == ["yawned"]
    assert not any(w.get("emph") for w in words)            # input untouched


def test_clean_hashtags_fixes_missing_hash_dedupes_and_tops_up():
    tags = blocky.clean_hashtags(["Viral", "#viral", "what if!", "#Fun_2"])
    assert tags[:3] == ["#Viral", "#whatif", "#Fun_2"]
    assert 8 <= len(tags) <= 12
    validate({"title": "t", "description": "d", "hashtags": tags}, "clip_metadata")
    assert len(blocky.clean_hashtags([f"#t{i}" for i in range(15)])) == 12


def test_spread_words_fills_the_span_in_order_by_length():
    w = blocky.spread_words("In this town", 1.0, 2.5)
    assert [x["word"].strip() for x in w] == ["In", "this", "town"]
    assert w[0]["start"] == 1.0 and w[-1]["end"] <= 2.5
    assert all(a["start"] < b["start"] for a, b in zip(w, w[1:]))
    assert (w[2]["start"] - w[1]["start"]) > (w[1]["start"] - w[0]["start"])
    assert blocky.spread_words("", 0, 1) == [] and blocky.spread_words("a", 2, 1) == []


def test_voiced_span_finds_speech_inside_silence(tmp_path):
    import numpy as np
    import soundfile as sf
    sr = 24000
    x = np.zeros(sr * 2, dtype="float32")
    x[int(0.5 * sr):int(1.5 * sr)] = 0.3 * np.sin(np.arange(sr) * 0.1)
    sf.write(str(tmp_path / "l.wav"), x, sr)
    a, b = blocky.voiced_span(tmp_path / "l.wav")
    assert a == pytest.approx(0.5, abs=0.03) and b == pytest.approx(1.5, abs=0.03)


def test_pick_premise_skips_used_ones_case_insensitively():
    seeds = ["What if A?", "What if B?"]
    assert blocky.pick_premise(seeds, ["what if a?"], random.Random(1)) == "What if B?"
    assert blocky.pick_premise(seeds, ["What if A?", "What if B?"], random.Random(1)) is None


def test_renderer_draws_every_option_the_schema_allows():
    js = (Path(blocky.ASSET_DIR) / "blocky.js").read_text(encoding="utf-8")

    def js_list(name):
        m = re.search(rf"const {name} = \[([^\]]*)\]", js)
        return re.findall(r"'([a-z_]+)'", m.group(1))

    assert set(schemas.BLOCKY_CAMERAS) == set(js_list("CAMERAS"))
    assert set(schemas.BLOCKY_FACES) <= set(js_list("FACE_EXPRS"))
    for place in schemas.BLOCKY_PLACES:
        assert re.search(rf"\n  {place}\(g, rnd\)", js), place
    for action in schemas.BLOCKY_ACTIONS:
        assert action == "idle" or f"case '{action}'" in js, action
    for prop in schemas.BLOCKY_PROPS:
        assert prop == "none" or f"case '{prop}'" in js, prop
    for shape in schemas.BLOCKY_SHAPES:
        assert shape == "person" or f"case '{shape}'" in js, shape
    for c in schemas.BLOCKY_COLORS:
        assert re.search(rf"\b{c}: '#", js), c


def test_word_pop_captions_one_word_per_event_and_emphasis_tinted(tmp_path):
    cfg = load_config()
    words = [{"word": "In", "start": 0.0, "end": 0.2},
             {"word": "this", "start": 0.25, "end": 0.4},
             {"word": "town", "start": 0.45, "end": 0.8, "emph": True}]
    ass = tmp_path / "c.ass"
    captions.write_ass(words, ass, cfg, "word-pop", anchor=0.62)
    events = [ln for ln in ass.read_text(encoding="utf-8-sig").splitlines()
              if ln.startswith("Dialogue:")]
    assert len(events) == 3
    assert events[2].endswith("TOWN") and cfg["captions"]["presets"]["word-pop"][
        "highlight_color"] in events[2]
    assert cfg["captions"]["presets"]["word-pop"]["highlight_color"] not in events[0]


def test_lose_part_pops_only_while_parts_are_left():
    raw = _story()
    raw["cast"][0]["hair"] = "none"            # milo: armB, armA, legB = 3 parts
    for s in raw["shots"][:5]:
        for a in s["actors"]:
            if a["id"] == "milo":
                a["action"] = "lose_part"
    story = blocky.clean_story(raw)
    bounds = [i * 3.0 for i in range(len(story["shots"]) + 1)]
    spec = blocky.build_spec(story, bounds, [], 1080, 1920, 30, 5, 0.9)
    milo = [a for s in spec["shots"] for a in s["actors"]
            if a["cast"] == 0 and a["action"] == "lose_part"]
    assert len(milo) == 4 and sum("lose_t" in a for a in milo) == 3
    pops = [c for c in blocky.sfx_cues(story, spec, [], cap=50) if c["kind"] == "pop"]
    assert len(pops) == 3 + sum(s["sfx"] == "pop" for s in story["shots"])
    assert blocky.losable_parts({"shape": "car", "hair": "none"}) == 0


def test_align_words_keeps_script_text_and_takes_whisper_times():
    script = blocky.spread_words("I scoop twelve cones today", 0.0, 2.5)
    heard = [{"word": " I", "start": 0.1, "end": 0.2}, {"word": " scoop", "start": 0.3, "end": 0.6},
             {"word": " 12", "start": 0.7, "end": 0.9}, {"word": " cones", "start": 1.0, "end": 1.3},
             {"word": " today.", "start": 1.4, "end": 1.8},
             {"word": " Thanks", "start": 3.0, "end": 3.4}]          # hallucinated in silence
    out = blocky.align_words(script, heard)
    assert [w["word"].strip() for w in out] == ["I", "scoop", "twelve", "cones", "today"]
    assert out[1]["start"] == 0.3 and out[3]["start"] == 1.0
    assert 0.6 <= out[2]["start"] < 1.0                               # clamped between neighbours
    assert all(a["start"] <= b["start"] for a, b in zip(out, out[1:]))


def test_trim_line_removes_dead_air(tmp_path):
    import numpy as np
    import soundfile as sf
    sr = 24000
    x = np.zeros(sr * 2, dtype="float32")
    x[int(0.6 * sr):int(1.2 * sr)] = 0.3
    p = tmp_path / "l.wav"
    sf.write(str(p), x, sr)
    assert blocky.trim_line(p) == pytest.approx(0.72, abs=0.05)
