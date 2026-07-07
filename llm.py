"""LLM brain behind one interface.

    complete_json(task, schema_name, prompt, provider=None, context=None) -> dict

Providers:
  mock   — first-class, deterministic, schema-valid; default when no key.
  gemini — google-genai SDK (lazy import), structured output via response_schema.
  groq   — OpenAI-compatible HTTP endpoint via requests (no SDK).
  ollama — local HTTP endpoint via requests (no SDK).

`import llm` must work with zero provider SDKs installed (lazy imports only).
Every call retries with exponential backoff, then attempts JSON repair; if all
fails an LLMError is raised and the caller applies its deterministic fallback.
Output is ALWAYS validated against the named schema before being returned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from config import load_config
from errors import LLMError
from logutil import get_logger
from schemas import SCHEMAS, SchemaValidationError, validate

log = get_logger("llm")

PROVIDERS = ("mock", "gemini", "groq", "ollama")


def resolve_provider(cfg: dict | None = None, override: str | None = None) -> str:
    """auto -> gemini when GEMINI_API_KEY exists, else mock. Logged."""
    cfg = cfg or load_config()
    name = (override or cfg["llm"]["provider"]).lower()
    if name == "auto":
        name = "gemini" if os.environ.get("GEMINI_API_KEY") else "mock"
        log.info("provider 'auto' resolved to '%s' (GEMINI_API_KEY %s)",
                 name, "set" if name == "gemini" else "not set")
    if name not in PROVIDERS:
        raise LLMError(f"unknown provider '{name}' (valid: {PROVIDERS})")
    return name


def complete_json(task: str, schema_name: str, prompt: str,
                  provider: str | None = None, context: dict | None = None,
                  cfg: dict | None = None) -> dict:
    """Run an LLM task that must return JSON matching SCHEMAS[schema_name]."""
    cfg = cfg or load_config()
    schema = SCHEMAS[schema_name]
    name = resolve_provider(cfg, provider)
    retries = int(cfg["llm"].get("max_retries", 2))
    backoff = float(cfg["llm"].get("backoff_base_seconds", 1.5))

    t0 = time.perf_counter()
    last_err: Exception | None = None
    raw = None
    for attempt in range(retries + 1):
        p = prompt
        if attempt > 0:
            p = (prompt + "\n\nIMPORTANT: your previous answer was not valid. "
                 "Respond with ONLY a JSON object matching this schema exactly:\n"
                 + json.dumps(schema) + "\nExample shape:\n"
                 + json.dumps(synthesize_from_schema(schema, seed=task)))
        try:
            raw = _dispatch(name, task, schema, p, context, cfg)
            data = raw if isinstance(raw, dict) else _parse_json(raw)
            validate(data, schema_name)
            log.info("task=%s provider=%s ok in %.2fs (attempt %d)",
                     task, name, time.perf_counter() - t0, attempt + 1)
            return data
        except (LLMError, SchemaValidationError, ValueError) as e:
            last_err = e
            log.warning("task=%s provider=%s attempt %d failed: %s",
                        task, name, attempt + 1, e)
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))

    # Retry ladder exhausted — one last shot: JSON repair on the raw text.
    if isinstance(raw, str):
        try:
            data = _parse_json(repair_json(raw))
            validate(data, schema_name)
            log.info("task=%s provider=%s recovered via JSON repair", task, name)
            return data
        except (SchemaValidationError, ValueError):
            pass
    raise LLMError(f"task '{task}' failed after {retries + 1} attempts + repair",
                   detail=str(last_err))


def _dispatch(name, task, schema, prompt, context, cfg):
    if name == "mock":
        return _mock_complete(task, schema, prompt, context)
    if name == "gemini":
        return _gemini_complete(schema, prompt, cfg)
    if name == "groq":
        return _groq_complete(schema, prompt, cfg)
    if name == "ollama":
        return _ollama_complete(schema, prompt, cfg)
    raise LLMError(f"unknown provider {name}")


# ---------------------------------------------------------------- providers

def _gemini_complete(schema, prompt, cfg):
    try:
        from google import genai  # lazy: SDK optional
    except ImportError as e:
        raise LLMError("gemini selected but google-genai is not installed "
                       "(pip install google-genai)", detail=str(e))
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("gemini selected but GEMINI_API_KEY is not set")
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=cfg["llm"]["gemini_model"],
        contents=prompt,
        config={"response_mime_type": "application/json",
                "response_schema": _strip_unsupported(schema)},
    )
    return resp.text


def _groq_complete(schema, prompt, cfg):
    import requests  # always installed, still lazy for import-speed
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMError("groq selected but GROQ_API_KEY is not set")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": cfg["llm"]["groq_model"],
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_object"},
              "temperature": 0.4},
        timeout=60,
    )
    if r.status_code != 200:
        raise LLMError(f"groq HTTP {r.status_code}", detail=r.text[:500])
    return r.json()["choices"][0]["message"]["content"]


def _ollama_complete(schema, prompt, cfg):
    import requests
    base = os.environ.get("OLLAMA_BASE_URL", cfg["llm"].get(
        "ollama_base_url", "http://localhost:11434"))
    r = requests.post(
        f"{base}/api/generate",
        json={"model": cfg["llm"]["ollama_model"], "prompt": prompt,
              "format": _strip_unsupported(schema), "stream": False},
        timeout=300,
    )
    if r.status_code != 200:
        raise LLMError(f"ollama HTTP {r.status_code}", detail=r.text[:500])
    return r.json()["response"]


def _strip_unsupported(schema):
    """Remove JSON-schema keywords some providers reject (pattern, etc.)."""
    if isinstance(schema, dict):
        return {k: _strip_unsupported(v) for k, v in schema.items()
                if k not in ("pattern", "additionalProperties")}
    if isinstance(schema, list):
        return [_strip_unsupported(x) for x in schema]
    return schema


# ------------------------------------------------------------ mock provider

def _seed_float(seed: str, lo: float, hi: float) -> float:
    """Deterministic pseudo-value in [lo, hi] from a string seed."""
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return round(lo + (h / 0xFFFFFFFF) * (hi - lo), 2)


def _mock_complete(task, schema, prompt, context):
    """Deterministic, schema-valid synthesis. Task-aware when the caller
    passes structured `context`; otherwise generic schema-driven output."""
    context = context or {}
    if task == "highlight_candidates" and context.get("windows"):
        cands = []
        for w in context["windows"][:10]:
            text = (w.get("text") or "").strip()
            hook = text.split(".")[0][:120] or "An interesting moment"
            cands.append({
                "start": float(w["start"]), "end": float(w["end"]),
                "hook": hook,
                "reason": "mock: energetic segment with clear standalone context",
                "score": _seed_float(f"{task}|{w['start']}|{text[:64]}", 5.0, 9.5),
            })
        return {"candidates": cands}
    if task == "clip_score":
        seed = str(context.get("text", prompt))[:256]
        return {k: _seed_float(f"{k}|{seed}", 4.0, 9.5)
                for k in ("hook_strength", "retention", "clarity", "impact")}
    if task == "clip_metadata" and context.get("text") is not None:
        # Delegate shape to the same deterministic template the fallback uses,
        # imported lazily to avoid a circular import at module load.
        from metadata import template_metadata
        return template_metadata(context.get("hook", ""), context["text"])
    if task == "virality" and context.get("duration") is not None:
        from virality import rule_based_virality
        return rule_based_virality(context["text"], context.get("hook", ""),
                                   context["duration"])
    return synthesize_from_schema(schema, seed=task)


def synthesize_from_schema(schema: dict, seed: str = "x"):
    """Generate a minimal valid instance for any schema in schemas.py."""
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        out = {}
        props = schema.get("properties", {})
        for key in schema.get("required", list(props.keys())):
            out[key] = synthesize_from_schema(props.get(key, {}), f"{seed}.{key}")
        return out
    if t == "array":
        n = max(schema.get("minItems", 1), 1)
        return [synthesize_from_schema(schema.get("items", {}), f"{seed}[{i}]")
                for i in range(n)]
    if t == "number":
        lo = schema.get("minimum", schema.get("exclusiveMinimum", 0) + 0.1)
        hi = schema.get("maximum", lo + 30)
        return _seed_float(seed, float(lo), float(hi))
    if t == "integer":
        return int(schema.get("minimum", 1)) or 1
    if t == "string":
        pat = schema.get("pattern", "")
        if pat.startswith("^#"):
            return f"#mock{abs(hash(seed)) % 1000}"
        s = f"mock-{seed.split('.')[-1]}"
        return s[: schema.get("maxLength", 60)] or "mock"
    if t == "boolean":
        return True
    return "mock"


# ------------------------------------------------------------- JSON helpers

def _parse_json(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM returned JSON that is not an object")
    return data


def repair_json(text: str) -> str:
    """Best-effort cleanup of near-JSON LLM output."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        t = t[start:end + 1]
    t = re.sub(r",\s*([}\]])", r"\1", t)          # trailing commas
    t = t.replace("“", '"').replace("”", '"')  # smart quotes
    t = re.sub(r"(?<=[{,])\s*'([^']*)'\s*:", r'"\1":', t)  # single-quoted keys
    return t
