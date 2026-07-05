"""Idempotency (completion markers / --force), sentence building, and other
pipeline-level pure logic."""
import json

from pipeline import _Stages, _slug
from transcribe import build_sentences


def test_stage_marker_skips_second_run(tmp_path):
    timings = {}
    st = _Stages(tmp_path, force=False, timings=timings)
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        return {"value": 42}

    assert st.run("demo", work) == {"value": 42}
    assert st.run("demo", work) == {"value": 42}
    assert calls["n"] == 1                       # second run skipped
    assert timings["demo"]["status"] == "skipped"
    assert (tmp_path / ".done_demo.json").exists()


def test_stage_force_reruns(tmp_path):
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        return {"n": calls["n"]}

    _Stages(tmp_path, False, {}).run("demo", work)
    out = _Stages(tmp_path, True, {}).run("demo", work)
    assert calls["n"] == 2 and out == {"n": 2}


def test_stage_failure_leaves_no_marker(tmp_path):
    st = _Stages(tmp_path, False, {})
    try:
        st.run("boom", lambda: 1 / 0)
    except ZeroDivisionError:
        pass
    assert not (tmp_path / ".done_boom.json").exists()  # failed → re-runs next time


def test_marker_content_is_json(tmp_path):
    _Stages(tmp_path, False, {}).run("j", lambda: {"a": [1, 2]})
    data = json.loads((tmp_path / ".done_j.json").read_text())
    assert data == {"a": [1, 2]}


def test_slug_sanitizes():
    assert _slug("My Video (final) v2.mp4") == "My-Video-final-v2"
    assert _slug("https://youtu.be/abc") == "url"


def test_build_sentences_punctuation():
    words = [{"word": "Hello", "start": 0.0, "end": 0.3},
             {"word": "world.", "start": 0.35, "end": 0.7},
             {"word": "Next", "start": 1.0, "end": 1.2},
             {"word": "one!", "start": 1.3, "end": 1.6}]
    sents = build_sentences(words)
    assert len(sents) == 2
    assert sents[0]["text"] == "Hello world."
    assert sents[0]["start"] == 0.0 and sents[0]["end"] == 0.7


def test_build_sentences_runon_split():
    words = [{"word": f"w{i}", "start": i * 2.0, "end": i * 2.0 + 1.0}
             for i in range(40)]  # 80s with no punctuation
    sents = build_sentences(words)
    assert len(sents) > 1  # forced split at MAX_SENTENCE_SECONDS
    for s in sents:
        assert s["end"] - s["start"] <= 32.0


def test_build_sentences_empty():
    assert build_sentences([]) == []
