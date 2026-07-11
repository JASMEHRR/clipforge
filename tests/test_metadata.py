import pytest

from metadata import _trim_title, clean_tag, template_metadata, topic_hashtags
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


def test_reported_junk_words_are_dropped():
    # the exact leak from the field report: #done #cool #right #yeah #wearing
    for junk in ("done", "cool", "right", "yeah", "wearing", "know", "really"):
        assert clean_tag(junk) is None, f"{junk} should be filtered"
    for keep in ("kubernetes", "startup", "espresso", "iceland"):
        assert clean_tag(keep) == keep


def test_topic_hashtags_filter_and_cap():
    text = ("So yeah I was really wearing my new espresso machine setup and "
            "honestly the espresso and the grinder changed my morning routine, "
            "you know? Espresso espresso grinder routine barista.")
    tags = topic_hashtags(text)
    bare = [t.lstrip("#") for t in tags]
    assert "#espresso" in tags               # real topic kept
    assert not ({"yeah", "really", "wearing", "honestly", "know"} & set(bare))
    assert bare[-1] == "shorts" or "shorts" in bare
    assert 8 <= len(tags) <= 12
    assert bare == [b.lower() for b in bare]  # all lowercase
    assert len(bare) == len(set(bare))        # deduped


def test_upload_cap_keeps_topics_over_staples():
    # metadata puts topics first, so the upload 5-cap keeps them, not staples
    from upload_scheduler import clean_hashtags
    tags = topic_hashtags("espresso espresso grinder grinder barista latte "
                          "milk foam", )
    capped = [t.lstrip("#") for t in clean_hashtags(tags)]
    assert "espresso" in capped and capped[-1] == "shorts"
    assert len(capped) <= 6                   # 5 topics + shorts


def test_description_expands_not_repeats_and_no_hashtags():
    out = template_metadata(
        "This bridge should not exist",
        "This bridge should not exist. Engineers in Norway anchored it to the "
        "seabed with tension cables no one had tried before. #wild #crazy")
    assert "#" not in out["description"]       # hashtags stripped from prose
    # second sentence adds real detail beyond the title
    assert "Norway" in out["description"] or "cables" in out["description"]


def test_generate_metadata_mock_provider(cfg):
    from metadata import generate_metadata
    out = generate_metadata("some transcript words here", "A hook", cfg,
                            provider="mock")
    validate(out, "clip_metadata")
