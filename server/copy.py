"""User-facing language for the API layer: plain-language stage names and
one-sentence errors. The frontend renders these verbatim — nothing here may
contain jargon (job, provider, NVENC, yt-dlp, mock, ...)."""
from __future__ import annotations

from logutil import get_logger

log = get_logger("server")

# Plain-language stage names shown on the progress screen, keyed by the
# canonical progress.STAGES keys. Every key in progress.STAGES must be here
# (test_server_api asserts that) so a new pipeline stage can't silently ship
# with an internal name in the UI.
STAGE_LABELS: dict[str, str] = {
    "init": "Getting ready",
    "deps": "Checking tools",
    "model_download": "Downloading the speech model",
    "model_load": "Loading the speech model",
    "ingest": "Preparing your video",
    "transcribe": "Listening to the audio",
    "events": "Spotting big moments",
    "scenes": "Finding scene changes",
    "highlights": "Finding the best moments",
    "refine": "Polishing clip timing",
    "avatar": "Preparing your avatar host",
    "render": "Making your clips",
    "rescore": "Ranking your clips",
    "cleanup": "Tidying up",
    "done": "Done",
}


def stage_label(key: str) -> str:
    return STAGE_LABELS.get(key, "Working")


def friendly(e: Exception, context: str) -> str:
    """One plain sentence for the UI; the full traceback goes to
    cache/logs/ui.log via the shared file handler. Call inside an except."""
    log.exception("%s failed", context)
    first = str(e).strip().splitlines()[0][:200] if str(e).strip() \
        else type(e).__name__
    return (f"{context} didn't work: {first} "
            f"(full details saved to cache/logs/ui.log)")


def label_snapshot(snap: dict) -> dict:
    """Return a copy of a ProgressTracker snapshot with plain-language stage
    labels substituted in, ready to send over the WebSocket."""
    out = dict(snap)
    out["stages"] = [dict(row, label=stage_label(row["key"]))
                     for row in snap.get("stages", [])]
    return out
