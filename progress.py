"""Professional progress tracking: named stages, per-stage %, ETA, elapsed
time, current file, processing speed, and per-file sub-progress.

Design goals:
- Thread-safe (pipeline renders clips from a thread pool; the UI polls from
  the Gradio thread).
- Backward compatible: the tracker can emit the legacy
  ``progress_cb(stage, frac, msg)`` callback so batch.py / CLI keep working.
- Never lets the UI look frozen: ``snapshot()`` always returns fresh elapsed
  times even when no new events arrive, and a heartbeat thread can push
  periodic refreshes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from logutil import get_logger

_log = get_logger("progress")
_LOG_INTERVAL = 2.0  # seconds between ETA log lines per stage, avoids spam

# Canonical stage order for a job. Weights are the share of total progress a
# stage represents (they sum to 1.0). Setup-time stages get small weights and
# are skipped instantly when nothing needs doing.
STAGES: list[tuple[str, str, float]] = [
    # (key,            label,                      weight)
    ("init",           "Initializing",             0.01),
    ("deps",           "Checking dependencies",    0.01),
    ("model_download", "Downloading models",       0.05),
    ("model_load",     "Loading models",           0.03),
    ("ingest",         "Preparing media",          0.10),
    ("transcribe",     "Transcribing audio",       0.15),
    ("scenes",         "Detecting scenes",         0.04),
    ("highlights",     "Selecting highlights",     0.05),
    ("refine",         "Refining clip timelines",  0.03),
    ("render",         "Rendering clips",          0.43),
    ("rescore",        "Re-scoring clips",         0.04),
    ("cleanup",        "Cleaning temporary files", 0.01),
    ("done",           "Completed",                0.05),
]
_KEYS = [k for k, _, _ in STAGES]
_LABEL = {k: label for k, label, _ in STAGES}
_WEIGHT = {k: w for k, _, w in STAGES}

PENDING, RUNNING, DONE, SKIPPED, FAILED = (
    "pending", "running", "done", "skipped", "failed")


def _fmt_secs(s: float | None) -> str:
    if s is None or s < 0:
        return "--:--"
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


@dataclass
class _Stage:
    key: str
    state: str = PENDING
    fraction: float = 0.0
    message: str = ""
    current_file: str = ""
    speed: str = ""                 # e.g. "3.2 MB/s", "4.1x realtime"
    started: float | None = None
    finished: float | None = None
    # sub-items (per-file progress during multi-clip rendering)
    items: dict = field(default_factory=dict)   # name -> fraction 0..1
    last_logged: float = 0.0        # monotonic time of last ETA log line

    def elapsed(self, now: float) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or now) - self.started

    def eta(self, now: float) -> float | None:
        """Rate-based remaining-time estimate for the running stage."""
        if self.state != RUNNING or self.fraction <= 0.02:
            return None
        el = self.elapsed(now)
        if el < 1.0:
            return None
        rate = self.fraction / el
        return max(0.0, (1.0 - self.fraction) / rate) if rate > 0 else None


class ProgressTracker:
    """Central progress state for one job. All mutators are thread-safe.

    Typical use in the pipeline::

        tracker = ProgressTracker(legacy_cb=progress_cb)
        tracker.start("ingest", "downloading input")
        tracker.update("ingest", 0.4, "downloading 40%", speed="3.1 MB/s")
        tracker.finish("ingest")
    """

    def __init__(self, legacy_cb=None, on_change=None):
        self._lock = threading.RLock()
        self._stages = {k: _Stage(k) for k in _KEYS}
        self._legacy_cb = legacy_cb
        self._on_change = on_change
        self._t0 = time.monotonic()
        self._overall_hint: float | None = None

    # ---------------------------------------------------------- mutators

    def start(self, key: str, message: str = "", current_file: str = "") -> None:
        with self._lock:
            st = self._stages[key]
            st.state = RUNNING
            st.started = st.started or time.monotonic()
            st.message = message or st.message
            st.current_file = current_file or st.current_file
        self._emit(key)

    def update(self, key: str, fraction: float, message: str = "",
               current_file: str = "", speed: str = "") -> None:
        with self._lock:
            st = self._stages[key]
            if st.state == PENDING:
                st.state, st.started = RUNNING, time.monotonic()
            st.fraction = min(1.0, max(st.fraction, float(fraction)))
            if message:
                st.message = message
            if current_file:
                st.current_file = current_file
            if speed:
                st.speed = speed
            now = time.monotonic()
            if now - st.last_logged >= _LOG_INTERVAL:
                st.last_logged = now
                _log.info("stage %s: %.0f%% eta %s", key, st.fraction * 100,
                           _fmt_secs(st.eta(now)))
        self._emit(key)

    def item(self, key: str, name: str, fraction: float) -> None:
        """Per-file sub-progress (e.g. each clip during rendering)."""
        with self._lock:
            st = self._stages[key]
            st.items[name] = min(1.0, max(0.0, float(fraction)))
            if st.items:
                st.fraction = max(st.fraction,
                                  sum(st.items.values()) / len(st.items))
        self._emit(key)

    def finish(self, key: str, message: str = "") -> None:
        with self._lock:
            st = self._stages[key]
            st.state, st.fraction = DONE, 1.0
            st.finished = time.monotonic()
            if message:
                st.message = message
        self._emit(key)

    def skip(self, key: str, message: str = "cached — skipped") -> None:
        with self._lock:
            st = self._stages[key]
            st.state, st.fraction, st.message = SKIPPED, 1.0, message
            st.started = st.started or time.monotonic()
            st.finished = time.monotonic()
        self._emit(key)

    def fail(self, key: str, message: str) -> None:
        with self._lock:
            st = self._stages[key]
            st.state, st.message = FAILED, message
            st.finished = time.monotonic()
        self._emit(key)

    # ----------------------------------------------------------- readers

    def overall_fraction(self) -> float:
        with self._lock:
            total = sum(_WEIGHT[k] * self._stages[k].fraction for k in _KEYS)
            return min(1.0, total)

    def overall_eta(self) -> float | None:
        frac = self.overall_fraction()
        if frac <= 0.03:
            return None
        el = time.monotonic() - self._t0
        return max(0.0, el * (1.0 - frac) / frac)

    def snapshot(self) -> dict:
        """UI-ready structure: overall + per-stage rows (fresh timings)."""
        now = time.monotonic()
        with self._lock:
            rows = []
            for k in _KEYS:
                st = self._stages[k]
                rows.append({
                    "key": k, "label": _LABEL[k], "state": st.state,
                    "fraction": st.fraction, "message": st.message,
                    "current_file": st.current_file, "speed": st.speed,
                    "elapsed": st.elapsed(now), "eta": st.eta(now),
                    "items": dict(st.items),
                })
        return {"overall": self.overall_fraction(),
                "overall_eta": self.overall_eta(),
                "elapsed": now - self._t0, "stages": rows}

    def render_text(self, bar_width: int = 22) -> str:
        """Multi-line human-readable progress board for text UIs."""
        snap = self.snapshot()
        filled = int(snap["overall"] * bar_width)
        head = (f"[{'█' * filled}{'░' * (bar_width - filled)}] "
                f"{snap['overall'] * 100:3.0f}%  "
                f"elapsed {_fmt_secs(snap['elapsed'])}  "
                f"ETA {_fmt_secs(snap['overall_eta'])}")
        lines = [head, ""]
        icons = {PENDING: "·", RUNNING: "▶", DONE: "✓",
                 SKIPPED: "↷", FAILED: "✗"}
        for s in snap["stages"]:
            if s["state"] == PENDING:
                lines.append(f" {icons[PENDING]} {s['label']}")
                continue
            bits = [f" {icons[s['state']]} {s['label']:<26}"
                    f"{s['fraction'] * 100:3.0f}%"]
            if s["state"] == RUNNING:
                bits.append(f"  ETA {_fmt_secs(s['eta'])}")
            bits.append(f"  {_fmt_secs(s['elapsed'])}")
            if s["speed"]:
                bits.append(f"  {s['speed']}")
            lines.append("".join(bits))
            detail = s["message"] or s["current_file"]
            if s["state"] == RUNNING and detail:
                lines.append(f"      {detail[:96]}")
            if s["state"] == RUNNING and s["items"]:
                for name, f in sorted(s["items"].items()):
                    ib = int(f * 10)
                    lines.append(f"      {name:<20} "
                                 f"[{'█' * ib}{'░' * (10 - ib)}] {f * 100:3.0f}%")
        return "\n".join(lines)

    # ----------------------------------------------------------- plumbing

    def legacy_report(self, stage: str, frac: float, msg: str = "") -> None:
        """Adapter for code that still calls (stage, frac, msg) directly."""
        key = stage if stage in _WEIGHT else "render"
        self.update(key, frac, msg)

    def _emit(self, key: str) -> None:
        st = self._stages[key]
        if self._legacy_cb:
            try:
                self._legacy_cb(key, self.overall_fraction(),
                                st.message or _LABEL[key])
            except Exception:  # noqa: BLE001 — a UI callback must never kill a job
                pass
        if self._on_change:
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001
                pass
