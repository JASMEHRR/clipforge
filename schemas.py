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
        # v2 engagement-signals breakdown (optional; legacy records omit these)
        "band": {"type": "string", "enum": ["Strong", "Promising", "Weak"]},
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "score": {"type": "number", "minimum": 0, "maximum": 10},
                    "reason": {"type": "string"},
                },
                "required": ["name", "score", "reason"],
            },
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
                   "enum": ["queued", "running", "done", "failed",
                            "cancelled"]},
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

# --- Style refinement layer (feature/style-refiner) -----------------------

_HOOK_TYPE = {"type": "string",
              "enum": ["question", "shocking-statement", "curiosity-gap", "statement"]}

# style_profile.py output: averaged viral-Shorts style descriptor.
STYLE_PROFILE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "references": {"type": "array", "items": {"type": "string"}},
        "hook": {
            "type": "object",
            "properties": {
                "dominant_type": _HOOK_TYPE,
                "type_distribution": {
                    "type": "object",
                    "additionalProperties": {"type": "number", "minimum": 0},
                },
            },
            "required": ["dominant_type"],
            "additionalProperties": False,
        },
        "silence": {
            "type": "object",
            "properties": {
                "median_gap_s": {"type": "number", "minimum": 0},
                "p90_gap_s": {"type": "number", "minimum": 0},
            },
            "required": ["median_gap_s", "p90_gap_s"],
            "additionalProperties": False,
        },
        "pacing": {
            "type": "object",
            "properties": {
                "scene_cuts_per_min": {"type": "number", "minimum": 0},
                "words_per_sec": {"type": "number", "minimum": 0},
            },
            "required": ["scene_cuts_per_min", "words_per_sec"],
            "additionalProperties": False,
        },
        "ending": {
            "type": "object",
            "properties": {
                "resolves_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "cta_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["resolves_ratio", "cta_ratio"],
            "additionalProperties": False,
        },
        "captions": {
            "type": "object",
            "properties": {
                # Hard-clamped to the CAPTION POSITION LAW band [0.52, 0.66].
                "vertical_anchor": {"type": "number", "minimum": 0.52, "maximum": 0.66},
                "words_per_line": {"type": "integer", "minimum": 1, "maximum": 8},
                "emphasis": {"type": "string"},
            },
            "required": ["vertical_anchor", "words_per_line"],
            "additionalProperties": False,
        },
        "frames": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "references", "hook", "silence", "pacing", "ending", "captions"],
    "additionalProperties": False,
}

# subtitle_detect.py output: burned-in subtitle band detection for one source range.
SUBTITLE_DETECT_RESULT = {
    "type": "object",
    "properties": {
        "present": {"type": "boolean"},
        "band_top_pct": {"type": "number", "minimum": 0, "maximum": 1},
        "band_bottom_pct": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "sampled_frames": {"type": "integer", "minimum": 0},
    },
    "required": ["present", "band_top_pct", "band_bottom_pct", "confidence", "sampled_frames"],
    "additionalProperties": False,
}

# One kept source span [start, end] in the EditPlan segment list.
_SEGMENT = {
    "type": "array",
    "items": {"type": "number", "minimum": 0},
    "minItems": 2,
    "maxItems": 2,
}

# style_refiner.py output: per-clip timeline edit plan consumed by cut/reframe/captions.
EDIT_PLAN = {
    "type": "object",
    "properties": {
        "segments": {"type": "array", "items": _SEGMENT, "minItems": 1},
        # Word timeline already remapped into OUTPUT time (post pause-removal).
        "words": {"type": "array", "items": _WORD},
        "output_duration": {"type": "number", "minimum": 0},
        "start_action": {"type": "string",
                         "enum": ["keep", "trim_silence", "shift_to_hook"]},
        "ending_action": {"type": "string",
                          "enum": ["keep", "trim_tail", "extend_forward", "pull_back"]},
        "total_ms_removed": {"type": "number", "minimum": 0},
        "flags": {
            "type": "array",
            "items": {"type": "string",
                      "enum": ["weak_hook", "unresolved_ending", "subs_kept"]},
        },
        "zoom_punch": {"type": "boolean"},
        "fades": {
            "type": "object",
            "properties": {
                "audio_in_ms": {"type": "number", "minimum": 0},
                "audio_out_ms": {"type": "number", "minimum": 0},
                "video_out_ms": {"type": "number", "minimum": 0},
            },
            "required": ["audio_in_ms", "audio_out_ms", "video_out_ms"],
            "additionalProperties": False,
        },
        "captions_enabled": {"type": "boolean"},
        "caption_anchor": {"type": "number", "minimum": 0.52, "maximum": 0.66},
        "cta": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "text": {"type": "string"},
                "duration_s": {"type": "number", "minimum": 0},
            },
            "required": ["enabled", "text", "duration_s"],
            "additionalProperties": False,
        },
        "existing_subs": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "replace", "keep", "ignore"]},
                "decision": {"type": "string", "enum": ["none", "replace", "keep"]},
                "reason": {"type": "string"},
                # REPLACE: bottom fraction to crop above (0 = none).
                "bottom_exclusion_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                # KEEP: horizontal centering bias 0..1; -1 sentinel = no bias.
                "h_bias_center": {"type": "number", "minimum": -1, "maximum": 1},
            },
            "required": ["mode", "decision", "reason",
                         "bottom_exclusion_ratio", "h_bias_center"],
            "additionalProperties": False,
        },
    },
    "required": ["segments", "words", "output_duration", "start_action",
                 "ending_action", "total_ms_removed", "flags", "zoom_punch",
                 "fades", "captions_enabled", "caption_anchor", "cta", "existing_subs"],
    "additionalProperties": False,
}

# LLM classifier: is the clip's opening sentence a self-contained hook?
HOOK_CLASSIFY = {
    "type": "object",
    "properties": {
        "self_contained": {"type": "boolean"},
        "hook_type": _HOOK_TYPE,
        "reason": {"type": "string"},
    },
    "required": ["self_contained", "hook_type", "reason"],
    "additionalProperties": False,
}

# LLM classifier: does the clip's final sentence resolve the thought?
ENDING_CLASSIFY = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["complete", "reason"],
    "additionalProperties": False,
}

# --- Viral detection v2 (feature/viral-v2) --------------------------------

VIRAL_EVENT_TYPES = ["laughter", "strong_reaction", "physical_event", "reveal",
                     "expression_shift", "energy_spike", "profound_statement",
                     "conflict", "celebration", "other"]

# LLM output: notable moments within ONE chunk, timestamps as chunk-relative
# MM:SS strings (what video models reliably produce; converted to absolute
# seconds by video_events).
VIRAL_EVENTS = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": VIRAL_EVENT_TYPES},
                    "t_start": {"type": "string", "pattern": r"^\d{1,3}:\d{2}$"},
                    "t_end": {"type": "string", "pattern": r"^\d{1,3}:\d{2}$"},
                    "description": {"type": "string", "minLength": 1,
                                    "maxLength": 300},
                    "intensity_1_10": {"type": "number", "minimum": 1,
                                       "maximum": 10},
                    "actors_hint": {"type": "string", "maxLength": 120},
                },
                "required": ["type", "t_start", "t_end", "description",
                             "intensity_1_10"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["events"],
    "additionalProperties": False,
}

# Merged per-job event timeline in ABSOLUTE source seconds; the contract
# consumed by highlights fusion, reframe, and metadata.json.
EVENT_TIMELINE = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": VIRAL_EVENT_TYPES},
                    "t_start_s": {"type": "number", "minimum": 0},
                    "t_end_s": {"type": "number", "minimum": 0},
                    "description": {"type": "string"},
                    "intensity_1_10": {"type": "number", "minimum": 1,
                                       "maximum": 10},
                    "actors_hint": {"type": "string"},
                    "source": {"type": "string",
                               "enum": ["gemini", "openrouter", "audio", "mock"]},
                },
                "required": ["type", "t_start_s", "t_end_s", "description",
                             "intensity_1_10", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["events"],
    "additionalProperties": False,
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
    "style_profile": STYLE_PROFILE,
    "subtitle_detect_result": SUBTITLE_DETECT_RESULT,
    "edit_plan": EDIT_PLAN,
    "hook_classify": HOOK_CLASSIFY,
    "ending_classify": ENDING_CLASSIFY,
    "viral_events": VIRAL_EVENTS,
    "event_timeline": EVENT_TIMELINE,
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
