"""Central JSON schema registry.

Every JSON structure that crosses a module boundary (or comes back from an LLM)
is defined here and validated with `validate()`. No module may emit JSON that
is not covered by a schema in SCHEMAS.
"""
from __future__ import annotations

import jsonschema

_WORD = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
    },
    "required": ["word", "start", "end"],
    "additionalProperties": False,
}

_SENTENCE = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "words": {"type": "array", "items": _WORD},
    },
    "required": ["text", "start", "end", "words"],
    "additionalProperties": False,
}

INGEST_INFO = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "source_type": {"type": "string", "enum": ["file", "url"]},
        "duration": {"type": "number", "minimum": 0},
        "width": {"type": "integer", "minimum": 1},
        "height": {"type": "integer", "minimum": 1},
        "fps": {"type": "number", "exclusiveMinimum": 0},
        "video_path": {"type": "string"},
        "audio_path": {"type": "string"},
    },
    "required": ["source", "source_type", "duration", "width", "height",
                 "fps", "video_path", "audio_path"],
    "additionalProperties": False,
}

TRANSCRIPT = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "language": {"type": "string"},
        "duration": {"type": "number", "minimum": 0},
        "words": {"type": "array", "items": _WORD},
        "sentences": {"type": "array", "items": _SENTENCE},
    },
    "required": ["text", "language", "duration", "words", "sentences"],
    "additionalProperties": False,
}

SCENE_LIST = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                },
                "required": ["index", "start", "end"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scenes"],
    "additionalProperties": False,
}

# LLM output: candidate highlight clips.
HIGHLIGHT_CANDIDATES = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "hook": {"type": "string", "minLength": 1},
                    "reason": {"type": "string"},
                    "score": {"type": "number", "minimum": 0, "maximum": 10},
                },
                "required": ["start", "end", "hook", "reason", "score"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

# LLM output: post-render clip re-scoring.
CLIP_SCORE = {
    "type": "object",
    "properties": {
        "hook_strength": {"type": "number", "minimum": 1, "maximum": 10},
        "retention": {"type": "number", "minimum": 1, "maximum": 10},
        "clarity": {"type": "number", "minimum": 1, "maximum": 10},
        "impact": {"type": "number", "minimum": 1, "maximum": 10},
    },
    "required": ["hook_strength", "retention", "clarity", "impact"],
    "additionalProperties": False,
}

# LLM output: per-clip social metadata.
CLIP_METADATA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 60},
        "description": {"type": "string", "minLength": 1},
        "hashtags": {
            "type": "array",
            "items": {"type": "string", "pattern": "^#[A-Za-z0-9_]+$"},
            "minItems": 8,
            "maxItems": 12,
        },
    },
    "required": ["title", "description", "hashtags"],
    "additionalProperties": False,
}

# LLM output: per-clip virality rating.
VIRALITY = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["post", "maybe", "skip"]},
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
    },
    "required": ["score", "verdict", "reasons"],
    "additionalProperties": False,
}

JOB_RECORD = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "created": {"type": "string"},
        "source": {"type": "string"},
        "status": {"type": "string",
                   "enum": ["queued", "running", "done", "failed"]},
        "settings": {"type": "object"},
        "stages": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "status": {"type": "string",
                               "enum": ["done", "skipped", "failed"]},
                    "seconds": {"type": "number", "minimum": 0},
                },
                "required": ["status", "seconds"],
            },
        },
        "clips": {"type": "array", "items": {"type": "object"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["job_id", "created", "source", "status", "settings",
                 "stages", "clips", "notes"],
    "additionalProperties": True,
}

SCHEMAS: dict[str, dict] = {
    "ingest_info": INGEST_INFO,
    "transcript": TRANSCRIPT,
    "scene_list": SCENE_LIST,
    "highlight_candidates": HIGHLIGHT_CANDIDATES,
    "clip_score": CLIP_SCORE,
    "clip_metadata": CLIP_METADATA,
    "virality": VIRALITY,
    "job_record": JOB_RECORD,
}


class SchemaValidationError(ValueError):
    def __init__(self, schema_name: str, message: str):
        self.schema_name = schema_name
        super().__init__(f"schema '{schema_name}': {message}")


def validate(instance, schema_name: str) -> None:
    """Raise SchemaValidationError if instance does not match the named schema."""
    schema = SCHEMAS[schema_name]
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as e:
        raise SchemaValidationError(schema_name, e.message) from e


def is_valid(instance, schema_name: str) -> bool:
    try:
        validate(instance, schema_name)
        return True
    except SchemaValidationError:
        return False
