import os

import pytest

import llm
from errors import LLMError
from schemas import SCHEMAS, validate


def test_mock_valid_for_every_schema(cfg):
    for name in SCHEMAS:
        out = llm.complete_json(name, name, f"test {name}", provider="mock",
                                cfg=cfg)
        validate(out, name)


def test_mock_is_deterministic(cfg):
    a = llm.complete_json("clip_score", "clip_score", "same prompt",
                          provider="mock", context={"text": "abc"}, cfg=cfg)
    b = llm.complete_json("clip_score", "clip_score", "same prompt",
                          provider="mock", context={"text": "abc"}, cfg=cfg)
    assert a == b


def test_resolve_auto_keyless(cfg, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm.resolve_provider(cfg, "auto") == "mock"


def test_resolve_auto_with_key(cfg, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    assert llm.resolve_provider(cfg, "auto") == "gemini"


def test_resolve_unknown_raises(cfg):
    with pytest.raises(LLMError):
        llm.resolve_provider(cfg, "nonsense")


def test_retry_then_success(cfg, monkeypatch):
    calls = {"n": 0}

    def flaky(name, task, schema, prompt, context, c):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"
        return {"hook_strength": 5, "retention": 5, "clarity": 5, "impact": 5}

    monkeypatch.setattr(llm, "_dispatch", flaky)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    out = llm.complete_json("clip_score", "clip_score", "p", provider="mock",
                            cfg=cfg)
    assert calls["n"] == 2 and out["retention"] == 5


def test_repair_recovers_fenced_json(cfg, monkeypatch):
    raw = ('```json\n{"hook_strength": 5, "retention": 5, "clarity": 5, '
           '"impact": 5,}\n```')
    monkeypatch.setattr(llm, "_dispatch",
                        lambda *a: raw)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    out = llm.complete_json("clip_score", "clip_score", "p", provider="mock",
                            cfg=cfg)
    assert out["impact"] == 5


def test_exhausted_raises_llmerror(cfg, monkeypatch):
    monkeypatch.setattr(llm, "_dispatch", lambda *a: "garbage {{{")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(LLMError):
        llm.complete_json("clip_score", "clip_score", "p", provider="mock",
                          cfg=cfg)


def test_gemini_without_sdk_raises_clean_error(cfg, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    # google-genai is intentionally NOT installed in the build env
    with pytest.raises(LLMError):
        llm._gemini_complete(SCHEMAS["clip_score"], "p", cfg)


def test_repair_json_variants():
    assert llm.repair_json("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert llm.repair_json('noise {"a": 1,} trailing') == '{"a": 1}'
    assert "“" not in llm.repair_json('{“a”: 1}')


def test_synthesize_hashtag_pattern():
    out = llm.synthesize_from_schema(SCHEMAS["clip_metadata"], seed="t")
    validate(out, "clip_metadata")
