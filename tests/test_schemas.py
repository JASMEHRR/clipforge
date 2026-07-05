import pytest

from schemas import SCHEMAS, SchemaValidationError, is_valid, validate


def test_all_schemas_have_type():
    for name, schema in SCHEMAS.items():
        assert schema.get("type") == "object", name


def test_clip_metadata_valid():
    validate({"title": "T", "description": "D.",
              "hashtags": [f"#t{i}" for i in range(8)]}, "clip_metadata")


@pytest.mark.parametrize("bad", [
    {"title": "x" * 61, "description": "D.", "hashtags": ["#a"] * 8},   # long title
    {"title": "T", "description": "D.", "hashtags": ["#a"] * 7},        # too few tags
    {"title": "T", "description": "D.", "hashtags": ["#a"] * 13},       # too many
    {"title": "T", "description": "D.", "hashtags": ["no-hash"] * 8},   # bad pattern
    {"title": "T", "description": "D."},                                # missing
])
def test_clip_metadata_invalid(bad):
    with pytest.raises(SchemaValidationError):
        validate(bad, "clip_metadata")


def test_highlight_candidates_bounds():
    ok = {"candidates": [{"start": 0, "end": 30, "hook": "h", "reason": "r",
                          "score": 10}]}
    validate(ok, "highlight_candidates")
    bad = {"candidates": [{"start": 0, "end": 30, "hook": "h", "reason": "r",
                           "score": 11}]}
    assert not is_valid(bad, "highlight_candidates")


def test_transcript_empty_is_valid():
    validate({"text": "", "language": "en", "duration": 0.0,
              "words": [], "sentences": []}, "transcript")
