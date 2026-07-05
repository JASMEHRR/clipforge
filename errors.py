"""Structured errors. Modules raise these; the pipeline catches them per stage
so one bad clip or stage never kills the whole run (or the batch queue)."""
from __future__ import annotations


class ClipForgeError(Exception):
    """Base structured error: stage + message + optional detail payload."""

    stage = "general"

    def __init__(self, message: str, detail: str | None = None):
        self.message = message
        self.detail = detail
        super().__init__(f"[{self.stage}] {message}" + (f" — {detail}" if detail else ""))


class ConfigError(ClipForgeError):
    stage = "config"


class IngestError(ClipForgeError):
    stage = "ingest"


class TranscribeError(ClipForgeError):
    stage = "transcribe"


class SceneError(ClipForgeError):
    stage = "scenes"


class HighlightError(ClipForgeError):
    stage = "highlights"


class CutError(ClipForgeError):
    stage = "cut"


class ReframeError(ClipForgeError):
    stage = "reframe"


class CaptionError(ClipForgeError):
    stage = "captions"


class MetadataError(ClipForgeError):
    stage = "metadata"


class LLMError(ClipForgeError):
    stage = "llm"


class UploadError(ClipForgeError):
    stage = "upload"


class UploadQuotaError(UploadError):
    """YouTube API quota exhausted — surfaced as a friendly message, never a crash."""
