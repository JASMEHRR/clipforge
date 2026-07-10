"""LLM brain behind one interface.

    complete_json(task, schema_name, prompt, provider=None, context=None) -> dict

Providers:
  mock       — first-class, deterministic, schema-valid; default when no key.
  gemini     — google-genai SDK (lazy import), structured output via
               response_schema; also the only provider with Files-API video
               upload (upload_media) for viral_v2.
  groq       — OpenAI-compatible HTTP endpoint via requests (no SDK).
  ollama     — local HTTP endpoint via requests (no SDK).
  openrouter — OpenAI-compatible HTTP endpoint via requests (no SDK);
               supports image parts (viral_v2 frame-batch fallback).

Multimodal: pass `media=[...]` to complete_json. Parts are provider-neutral:
  {"kind": "gemini_file", "handle": <files.upload result>}   (gemini only)
  {"kind": "image", "mime": "image/jpeg", "data": <bytes>}   (gemini/openrouter)
mock ignores media; groq/ollama raise LLMError when media is passed.

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

PROVIDERS = ("mock", "gemini", "groq", "ollama", "openrouter")


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
                  cfg: dict | None = None,
                  media: list[dict] | None = None) -> dict:
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
            raw = _dispatch(name, task, schema, p, context, cfg, media)
            data = raw if isinstance(raw, dict) else _parse_json(raw)
            validate(data, schema_name)
            log.info("task=%s provider=%s ok in %.2fs (attempt %d)",
                     task, name, time.perf_counter() - t0, attempt + 1)
            return data
        except (LLMError, SchemaValidationError, ValueError) as e:
            last_err = e
            log.warning("task=%s provider=%s attempt %d failed: %s",
                        task, name, attempt + 1, e)
            if attempt < retries and getattr(e, "retryable", True):
                time.sleep(backoff * (2 ** attempt))
            elif attempt < retries:
                break  # non-retryable — stop burning attempts, fall through to repair/raise

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


def _dispatch(name, task, schema, prompt, context, cfg, media=None):
    if name == "mock":
        return _mock_complete(task, schema, prompt, context)
    if name == "gemini":
        return _gemini_complete(schema, prompt, cfg, media)
    if name == "groq":
        if media:
            raise LLMError("groq does not support media input")
        return _groq_complete(schema, prompt, cfg)
    if name == "ollama":
        if media:
            raise LLMError("ollama does not support media input")
        return _ollama_complete(schema, prompt, cfg)
    if name == "openrouter":
        return _openrouter_complete(schema, prompt, cfg, media)
    raise LLMError(f"unknown provider {name}")


# ---------------------------------------------------------------- providers

def _gemini_client():
    try:
        from google import genai  # lazy: SDK optional
    except ImportError as e:
        raise LLMError("gemini selected but google-genai is not installed "
                       "(pip install google-genai)", detail=str(e))
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("gemini selected but GEMINI_API_KEY is not set")
    return genai.Client(api_key=key)


_RETRYABLE_TOKENS = ("503", "500", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                     "429", "DEADLINE_EXCEEDED", "timeout", "Timeout", "Connection")
_NONRETRYABLE_TOKENS = ("400", "401", "403", "404")


def _classify_gemini_error(e: Exception) -> LLMError:
    """Wrap a raw google-genai SDK exception into LLMError so it flows through
    complete_json's existing retry loop instead of crashing unwrapped. 503/500/
    429/timeouts are retryable; 400/401/403/404 are not (same sniffing style as
    video_events._is_quota_error since the SDK's error types aren't stable to
    import across versions)."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    s = f"{type(e).__name__} {e}"
    if code in (400, 401, 403, 404):
        retryable = False
    elif code in (429, 500, 503, 504):
        retryable = True
    elif any(tok in s for tok in _NONRETRYABLE_TOKENS):
        retryable = False
    else:
        retryable = any(tok in s for tok in _RETRYABLE_TOKENS)
    return LLMError(f"gemini request failed: {s}", detail=str(e), retryable=retryable)


def _gemini_complete(schema, prompt, cfg, media=None):
    client = _gemini_client()
    config = {"response_mime_type": "application/json",
              "response_schema": _strip_unsupported(schema)}
    contents = prompt
    if media:
        from google.genai import types  # lazy, same SDK as above
        parts = []
        for m in media:
            if m["kind"] == "gemini_file":
                parts.append(m["handle"])
            elif m["kind"] == "image":
                parts.append(types.Part.from_bytes(data=m["data"],
                                                   mime_type=m["mime"]))
            else:
                raise LLMError(f"unknown media kind '{m.get('kind')}'")
        contents = parts + [prompt]
        # Video tokens dominate cost; low resolution is plenty for event spotting.
        config["media_resolution"] = "MEDIA_RESOLUTION_LOW"
    try:
        resp = client.models.generate_content(
            model=cfg["llm"]["gemini_model"], contents=contents, config=config)
    except LLMError:
        raise
    except Exception as e:  # raw google-genai SDK errors (ServerError/ClientError/...)
        raise _classify_gemini_error(e) from e
    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        log.info("gemini tokens: prompt=%s candidates=%s total=%s",
                 getattr(usage, "prompt_token_count", "?"),
                 getattr(usage, "candidates_token_count", "?"),
                 getattr(usage, "total_token_count", "?"))
    return resp.text


def upload_media(path, cfg=None, timeout_s: float = 120.0):
    """Upload a local media file via the Gemini Files API and wait until it is
    ACTIVE (required before it can be referenced in a prompt). Returns the file
    handle for use as a {"kind": "gemini_file", "handle": ...} media part."""
    client = _gemini_client()
    try:
        f = client.files.upload(file=str(path))
    except Exception as e:  # SDK raises assorted google.* error types
        raise _classify_gemini_error(e) from e
    t0 = time.perf_counter()
    delay = 1.0
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        if time.perf_counter() - t0 > timeout_s:
            raise LLMError(f"gemini file {f.name} not ACTIVE after {timeout_s:.0f}s")
        time.sleep(delay)
        delay = min(delay * 1.5, 10.0)
        try:
            f = client.files.get(name=f.name)
        except Exception as e:
            raise LLMError(f"gemini file poll failed for {f.name}", detail=str(e))
    state = getattr(f.state, "name", str(f.state))
    if state != "ACTIVE":
        raise LLMError(f"gemini file {f.name} ended in state {state}")
    return f


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


def _openrouter_complete(schema, prompt, cfg, media=None):
    import requests
    import base64
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("openrouter selected but OPENROUTER_API_KEY is not set")
    model = cfg["llm"].get("openrouter_model", "")
    if not model:
        raise LLMError("llm.openrouter_model is not set in config.yaml")
    content: str | list = prompt
    if media:
        content = [{"type": "text", "text": prompt}]
        for m in media:
            if m["kind"] != "image":
                raise LLMError(f"openrouter supports only image media, "
                               f"got '{m.get('kind')}'")
            b64 = base64.b64encode(m["data"]).decode("ascii")
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{m['mime']};base64,{b64}"}})
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model,
              "messages": [{"role": "user", "content": content}],
              "response_format": {"type": "json_object"},
              "temperature": 0.4},
        timeout=120,
    )
    if r.status_code != 200:
        raise LLMError(f"openrouter HTTP {r.status_code}", detail=r.text[:500])
    body = r.json()
    usage = body.get("usage") or {}
    if usage:
        log.info("openrouter tokens: prompt=%s completion=%s total=%s",
                 usage.get("prompt_tokens", "?"),
                 usage.get("completion_tokens", "?"),
                 usage.get("total_tokens", "?"))
    return body["choices"][0]["message"]["content"]


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
    if task == "viral_events":
        # Deterministic canned events per chunk so keyless gates exercise the
        # full events -> fusion -> metadata path.
        dur = float(context.get("chunk_seconds", 60.0))
        seed = f"{task}|{context.get('chunk_start', 0)}|{dur}"

        def _mmss(t: float) -> str:
            t = max(0.0, min(t, dur))
            return f"{int(t // 60)}:{int(t % 60):02d}"

        events = []
        for frac, etype, desc in ((0.25, "laughter",
                                   "mock: group laughter burst"),
                                  (0.70, "energy_spike",
                                   "mock: sudden energy spike")):
            t0 = dur * frac
            events.append({
                "type": etype,
                "t_start": _mmss(t0),
                "t_end": _mmss(t0 + 4.0),
                "description": desc,
                "intensity_1_10": _seed_float(f"{seed}|{etype}", 5.0, 9.0),
                "actors_hint": "person speaking",
            })
        return {"events": events}
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
