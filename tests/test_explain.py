"""Explainer mode: pure-logic checks (cue sync, number format, mock script)."""
import pytest

import explain
import llm
import slides
from schemas import validate


def _words(text, t0=1.0, step=0.2):
    return [{"word": f" {w}", "start": t0 + i * step} for i, w in enumerate(text.split())]


def test_cue_times_follow_the_spoken_words_in_order():
    w = _words("You can pay with a card, sign a lease, or take loans.")
    t = explain.cue_times(w, 0.9, 6.0, ["card", "lease", "loan"], lead=0.1)
    assert [round(x, 2) for x in t] == [1.9, 2.5, 3.1]          # "loans" matches "loan"


def test_cue_times_repeated_word_maps_to_the_next_mention():
    w = _words("a loan is a loan and a loan")
    t = explain.cue_times(w, 0.9, 5.0, ["loan", "loan"], lead=0.0)
    assert t == [1.2, 1.8]


def test_cue_times_fallbacks_stay_ordered_and_inside_the_slide():
    w = _words("nothing here matches the cues at all")
    t = explain.cue_times(w, 1.0, 4.0, ["alpha", "beta", "gamma"])
    assert all(1.0 < x < 4.0 for x in t) and t == sorted(t)
    assert min(b - a for a, b in zip(t, t[1:])) >= 0.3 - 1e-9
    multi = explain.cue_times(_words("they pay monthly payments"), 0.9, 3.0,
                              ["huge monthly bills"], lead=0.0)
    assert multi == [1.4]                                       # longest word fallback


def test_fmt_value_keeps_the_final_precision_while_counting():
    assert slides.fmt_value(30000, "$") == "$30,000"
    assert slides.fmt_value(21.7, "%", 22) == "22%"
    assert slides.fmt_value(4.5, "%", 4.5) == "4.5%"
    assert slides.fmt_value(12, "none") == "12"


def test_mock_explainer_is_schema_valid_and_object_seed_is_stable():
    validate(llm._mock_explainer_script("paying for a car"), "explainer_script")
    assert explain.object_seed("Car ") == explain.object_seed("car")
    assert explain.object_seed("car") != explain.object_seed("house")


def test_highlight_span_and_sticker_time():
    assert slides.highlight_span("Two meetings, one new direction", "ONE NEW direction") == (14, 31)
    h = "Paying for a car"
    a, b = slides.highlight_span(h, "not in heading")
    assert h[a:b] == "car"                                       # falls back to last word
    items = [{"at": 2.0}, {"at": 5.0}]
    assert slides.sticker_time(items, 1.0, 9.0) == 5.8
    assert slides.sticker_time(items, 1.0, 6.0) == 5.3           # stays inside the slide


def test_sfx_cues_and_sounds():
    plan = [{"layout": "big_number", "items": [{"at": 1.0}], "sticker": ""},
            {"layout": "list", "items": [{"at": 4.0}, {"at": 5.0}], "sticker": "BUSTED"}]
    cues = explain.sfx_cues(plan, [0.0, 3.0, 9.0])
    assert [k for _, k in cues] == ["slap", "kaching", "whoosh", "slap", "slap", "stamp"]
    for kind in explain.SFX_KINDS:
        snd = explain.synth_sfx(kind, 24000)
        assert 0 < abs(snd).max() <= 0.25                       # quiet under the voice
        assert (snd == explain.synth_sfx(kind, 24000)).all()    # deterministic


def test_cast_faces_cover_every_mood_and_people_differ():
    from schemas import EXPLAIN_MOODS
    assert set(explain.MOOD_FACES) == set(EXPLAIN_MOODS)
    people = explain.cast_people([
        {"name": "Maya", "look": "a woman in her 30s, yellow sweater"},
        {"name": "Jake", "look": "Adult man with curly hair"},
        {"name": "Ana", "look": "young woman, glasses"},
        {"name": "Sam", "look": "adult in a green coat"}])
    assert people == {"maya": ("Woman office worker", "Medium-Light"),
                      "jake": ("Man technologist", "Medium-Dark"),
                      "ana": ("Woman student", "Light"),
                      "sam": ("Office worker", "Medium")}
    assert len({p for p, _ in people.values()}) == 4                 # nobody looks alike


def test_join_number_pieces():
    w = [{"word": " costs", "start": 0.0, "end": 0.3},
         {"word": " $30", "start": 0.3, "end": 0.6},
         {"word": ",000.", "start": 0.6, "end": 0.9},
         {"word": " at", "start": 1.0, "end": 1.1},
         {"word": " 24", "start": 1.1, "end": 1.3},
         {"word": "%", "start": 1.3, "end": 1.4}]
    out = explain.join_number_pieces(w)
    assert [x["word"] for x in out] == [" costs", " $30,000.", " at", " 24%"]
    assert out[1]["start"] == 0.3 and out[1]["end"] == 0.9
    assert explain.join_number_pieces([{"word": " a", "start": 0, "end": 1},
                                       {"word": ".", "start": 1, "end": 2}])[1]["word"] == "."


def test_tighten_times_removes_dead_air_but_never_delays():
    # first item late, a long wait before the third
    assert explain.tighten_times([4.0, 5.0, 12.0], 1.0, 0.9, 4.5) == [1.9, 5.0, 9.5]
    # already tight: untouched
    assert explain.tighten_times([1.2, 2.0], 1.0, 0.9, 4.5) == [1.2, 2.0]
    # a pulled-forward first item drags a far-away second one to within max_gap
    assert explain.tighten_times([9.0, 9.1], 1.0, 0.35, 4.5) == pytest.approx([1.35, 5.85])
    assert explain.tighten_times([], 1.0, 0.9, 4.5) == []


def test_judge_score_and_rating():
    j = {"hook": 8, "clarity": 8, "payoff": 7, "fun": 6, "safe": True, "problems": ["weak end"],
         "fix": "x"}
    assert explain.judge_score(j) == 74.0                      # 28 + 20 + 14 + 12
    assert explain.judge_score({**j, "safe": False}) == 0.0
    cfg = {"explain": {"judge": {"min_score": 70}}, "create": {"virality_score": 60}}
    good = explain.rating({**j, "score": 74.0}, cfg)
    assert good["verdict"] == "post" and good["score"] == 74.0
    validate(good, "virality")
    assert explain.rating({**j, "score": 55.0}, cfg)["verdict"] == "skip"
    assert explain.passes(None, cfg) and not explain.passes({**j, "score": 55.0}, cfg)


def test_scout_rank_and_parsers():
    import scout
    a = [{"id": "a1", "title": "NVIDIA unveils new graphics card today", "source": "A"},
         {"id": "a2", "title": "Used story", "source": "A"}]
    b = [{"id": "b1", "title": "Nvidia unveils a new graphics card", "source": "B"},   # same story
         {"id": "b2", "title": "Game studio delays big release again", "source": "B"}]
    out = scout.rank([a, b], ["a2"], 10)
    assert [c["id"] for c in out] == ["a1", "b2"]
    assert scout.strip_html("<p>Hi <b>there</b></p><script>x()</script> &amp; you") == "Hi there & you"
    hn = scout.parse_hn(b'{"hits": [{"objectID": "1", "title": "Low", "points": 5, "url": "u"},'
                        b' {"objectID": "2", "title": "High", "points": 50}]}')
    assert [h["title"] for h in hn] == ["High", "Low"]


def test_studio_commands():
    from server.routes_studio import StudioRun, command, readable
    assert command(StudioRun(kind="money"))[1:] == ["explain.py", "--count", "1"]
    assert command(StudioRun(kind="money", topic=" debt "))[-2:] == ["--topic", "debt"]
    assert command(StudioRun(kind="trending", count=3))[1:] == ["scout.py", "--make", "3"]
    story = command(StudioRun(kind="story", text="a post", author=""))
    assert story[1:4] == ["explain.py", "--story", "a post"] and "Someone" in story
    with pytest.raises(ValueError):
        command(StudioRun(kind="story", text="  "))
    with pytest.raises(ValueError):
        command(StudioRun(kind="nope"))
    assert readable(["", "x ffmpeg error -i a.mp4", "stage voice: done"]) == ["stage voice: done"]


def test_scout_bans_private_lives_and_allegations():
    import scout
    for bad in ["Nvidia and Jensen Huang sued by shareholders over affair with alleged spy",
                "Jensen Huang Hired a PR Firm Known for Handling Sex Scandals Last Year",
                "Man who lost $308,000 on sports bets sues Kalshi",
                "Democrats pursue oversight after election", "Founder dies at 54"]:
        assert scout.banned(bad), bad
    for ok in ["Steam now shows average FPS for thousands of Steam Deck games",
               "Rockstar Officially Confirms GTA 6 Regional Release Times",
               "SoftBank Logs $7 Billion Loss After US Data Center Costs Swell",
               "Sony Wins Hall Of Shame Award For Killing Discs"]:
        assert not scout.banned(ok) or "Killing" in ok, ok
    out = scout.rank([[{"id": "1", "title": "CEO sued over affair", "source": "A"},
                       {"id": "2", "title": "New console sells out in minutes", "source": "A"}]],
                     [], 10)
    assert [c["id"] for c in out] == ["2"]


def test_provider_ladder_skips_what_cannot_answer(monkeypatch):
    import create
    cfg = {"create": {"providers": ["groq", "ollama", "gemini"], "groq_models": ["g1"],
                      "ollama_model": "o1", "gemini_models": ["m1", "m2"]},
           "llm": {"ollama_base_url": "http://x"}}
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(create, "_ollama_up", lambda cfg: False)
    assert create.ladder(cfg, "gemini") == [("gemini", "m1"), ("gemini", "m2")]
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(create, "_ollama_up", lambda cfg: True)
    assert create.ladder(cfg, "gemini") == [("groq", "g1"), ("ollama", "o1"),
                                            ("gemini", "m1"), ("gemini", "m2")]
    assert create.ladder(cfg, "mock") == [("mock", None)]


def test_speakable_ranges_and_dashes():
    assert explain.speakable("Share price range: $70‑$78.") == "Share price range: $70 to $78."
    assert explain.speakable("from 2019-2024 it grew") == "from 2019 to 2024 it grew"
    assert explain.speakable("an island—maybe.") == "an island, maybe."
    assert explain.speakable("plain line") == "plain line"


def test_fmt_value_keeps_two_decimals_and_stamp_text_is_drawable():
    assert slides.fmt_value(1.75, "$", 1.75) == "$1.75"
    assert slides.fmt_value(1.7, "$", 1.75) == "$1.70"
    assert slides.fmt_value(1750000000, "$") == "$1,750,000,000"
    import numpy as np
    assert isinstance(slides.stamp_sprite("\U0001F4B0 PAYDAY!", 1.0, 1), np.ndarray)


def test_emoji_only_sticker_means_no_stamp():
    assert slides.stamp_text("\U0001F4B0\U0001F911") == ""
    assert slides.stamp_text("Pay‑day \U0001F4B0!") == "PAYDAY !"
