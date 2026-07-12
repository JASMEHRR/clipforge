"""Niche classifier tests — heuristic always works keyless; the LLM path is
only consulted on a real provider and never trusted off the allowed list."""
import pytest

import classify
import llm
from errors import LLMError


# ------------------------------------------------------------ heuristic ----

@pytest.mark.parametrize("text,expected", [
    ("welcome back to the podcast, my guest today gave a great interview",
     "podcast"),
    ("that joke was hilarious, the whole crowd was laughing at the punchline",
     "comedy"),
    ("insane clutch gameplay in that boss fight, what a speedrun", "gaming"),
    ("the championship final went to the tournament favorites this season",
     "sports"),
    ("how to invest in stocks and grow your portfolio with compound interest",
     "finance"),
    ("in this lesson we explain the physics experiment step by step",
     "education"),
    ("no way, watch this, I can't believe what just happened", "reactions"),
])
def test_rule_based_niche_hits_each_taxonomy_entry(text, expected):
    assert classify.rule_based_niche(text) == expected


def test_rule_based_niche_garbage_and_empty_fall_back_to_other():
    assert classify.rule_based_niche("asdf qwerty zxcv lorem ipsum") == "other"
    assert classify.rule_based_niche("") == "other"
    assert classify.rule_based_niche(None) == "other"


def test_rule_based_niche_title_and_hashtags_outweigh_transcript():
    # one transcript hit for finance vs one title hit (3x) for gaming
    niche = classify.rule_based_niche(
        "we talked about money for a second", "Insane GAMEPLAY moments",
        ["#gaming"])
    assert niche == "gaming"


# ---------------------------------------------------------- allowed list ----

def test_allowed_niches_merges_custom_deduped_lowercase():
    cfg = {"classify": {"custom_niches": ["Cooking", "cooking", "gaming", ""]}}
    allowed = classify.allowed_niches(cfg)
    assert allowed.count("cooking") == 1
    assert allowed.count("gaming") == 1
    assert allowed[:len(classify.NICHES)] == classify.NICHES


# ------------------------------------------------------------- LLM path ----

def test_classify_niche_mock_provider_never_calls_llm(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("complete_json must not be called under mock")
    monkeypatch.setattr(llm, "complete_json", boom)
    cfg = {"llm": {"provider": "mock"}}
    meta = {"title": "Stock market basics", "hashtags": ["#invest"]}
    assert classify.classify_niche("we invest in stocks", meta, cfg) == "finance"


def test_classify_niche_llm_failure_keeps_heuristic(monkeypatch):
    monkeypatch.setattr(llm, "resolve_provider", lambda cfg, p=None: "gemini")

    def fail(*a, **kw):
        raise LLMError("provider down")
    monkeypatch.setattr(llm, "complete_json", fail)
    cfg = {"llm": {"provider": "gemini"}}
    meta = {"title": "", "hashtags": []}
    assert classify.classify_niche("boss fight gameplay speedrun",
                                   meta, cfg) == "gaming"


def test_classify_niche_llm_answer_off_list_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "resolve_provider", lambda cfg, p=None: "gemini")
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **kw: {"niche": "underwater-basket-weaving"})
    cfg = {"llm": {"provider": "gemini"}}
    meta = {"title": "", "hashtags": []}
    assert classify.classify_niche("boss fight gameplay speedrun",
                                   meta, cfg) == "gaming"


def test_classify_niche_llm_answer_on_list_wins(monkeypatch):
    monkeypatch.setattr(llm, "resolve_provider", lambda cfg, p=None: "gemini")
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **kw: {"niche": "Comedy"})
    cfg = {"llm": {"provider": "gemini"}}
    meta = {"title": "", "hashtags": []}
    assert classify.classify_niche("boss fight gameplay speedrun",
                                   meta, cfg) == "comedy"


def test_classify_niche_custom_niche_accepted_from_llm(monkeypatch):
    monkeypatch.setattr(llm, "resolve_provider", lambda cfg, p=None: "gemini")
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **kw: {"niche": "cooking"})
    cfg = {"llm": {"provider": "gemini"},
           "classify": {"custom_niches": ["cooking"]}}
    meta = {"title": "", "hashtags": []}
    assert classify.classify_niche("some text", meta, cfg) == "cooking"


def test_classify_niche_disabled_returns_other():
    cfg = {"classify": {"enabled": False}, "llm": {"provider": "mock"}}
    meta = {"title": "Stock market", "hashtags": []}
    assert classify.classify_niche("invest in stocks", meta, cfg) == "other"


def test_mock_provider_niche_task_is_deterministic():
    out = llm.complete_json("niche", "niche", "classify this",
                            provider="mock",
                            cfg={"llm": {"provider": "mock",
                                         "max_retries": 0}})
    assert out == {"niche": "other"}
