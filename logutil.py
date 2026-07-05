"""Logging: timestamped, human-readable, per-stage timings.

Every module gets a logger via get_logger(); pipeline stages wrap work in
stage_timer() which logs start, summarized outcome, and elapsed seconds.
"""
from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-12s %(message)s"
_DATEFMT = "%H:%M:%S"
_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root = logging.getLogger("clipforge")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"clipforge.{name}")


def add_file_handler(log_path: str | Path) -> logging.Handler:
    """Attach a per-job log file; caller removes it with remove_file_handler."""
    _configure()
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
    logging.getLogger("clipforge").addHandler(fh)
    return fh


def remove_file_handler(handler: logging.Handler) -> None:
    logging.getLogger("clipforge").removeHandler(handler)
    handler.close()


@contextmanager
def stage_timer(logger: logging.Logger, stage: str, timings: dict | None = None):
    """Log stage start/end and record elapsed seconds into `timings` if given."""
    logger.info("stage %s: start", stage)
    t0 = time.perf_counter()
    try:
        yield
    except Exception as e:
        elapsed = time.perf_counter() - t0
        logger.error("stage %s: FAILED after %.1fs — %s", stage, elapsed, e)
        if timings is not None:
            timings[stage] = {"status": "failed", "seconds": round(elapsed, 2)}
        raise
    elapsed = time.perf_counter() - t0
    logger.info("stage %s: done in %.1fs", stage, elapsed)
    if timings is not None:
        timings[stage] = {"status": "done", "seconds": round(elapsed, 2)}
