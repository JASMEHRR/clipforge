"""Explainer mode: pure-logic checks (cue sync, number format, mock script)."""
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
