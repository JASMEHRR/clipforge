import pytest

from metadata import _trim_title, template_metadata
from schemas import validate

CASES = [
    ("Most projects fail.", "Most projects fail. They lack goals. Write one "
                            "sentence about who needs this and why it matters."),
    ("", ""),                                          # everything empty
    ("x" * 300, "word " * 500),                        # very long
    ("Ünïcödé hook!", "Ünïcödé wörds ünïcödé wörds."),  # unicode
    ("hook", "a b c"),                                 # tiny text
]


@pytest.mark.parametrize("hook,text", CASES)
def test_template_always_valid(hook, text):
    out = template_metadata(hook, text)
    validate(out, "clip_metadata")


def test_title_max_60():
    out = template_metadata("t" * 200, "some words here")
    assert len(out["title"]) <= 60


def test_trim_title_word_boundary():
    t = _trim_title("one two three " * 10)
    assert len(t) <= 60 and t.endswith("...")


def test_hashtags_from_keywords():
    out = template_metadata("hook", "kubernetes kubernetes kubernetes "
                                    "deployment deployment cluster")
    assert "#kubernetes" in out["hashtags"]
    assert 8 <= len(out["hashtags"]) <= 12


def test_description_two_parts():
    out = template_metadata("The hook line", "The hook line. The payoff line "
                                             "explains everything clearly.")
    assert out["description"].count(".") >= 2


def test_generate_metadata_mock_provider(cfg):
    from metadata import generate_metadata
    out = generate_metadata("some transcript words here", "A hook", cfg,
                            provider="mock")
    validate(out, "clip_metadata")
