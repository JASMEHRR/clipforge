"""In-memory run registry: one RunHandle per active pipeline run or clip
re-render, with live progress fan-out to WebSocket subscribers.

Restart loses the registry by design — finished jobs persist via job.json and
history.db; an interrupted run resumes through the existing .done_<stage>
markers when re-submitted.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from logutil import get_logger
from progress import ProgressTracker
from server.copy import label_snapshot

log = get_logger("server")

_LOOP: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the server's event loop at startup so tracker callbacks fired
    from pipeline worker threads can wake WebSocket subscribers."""
    global _LOOP
    _LOOP = loop


@dataclass
class RunHandle:
    id: str
    state: str = "running"            # running | done | error | cancelled
    tracker: ProgressTracker | None = None
    thread: threading.Thread | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None        # job dict / clip record on success
    error: str | None = None          # already-friendly sentence
    last_snapshot: dict | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict | None:
        with self.lock:
            return self.last_snapshot

    # -- subscriber management (called from the event loop thread) ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # -- called from the worker thread --------------------------------------
    def push(self, tracker: ProgressTracker) -> None:
        snap = label_snapshot(tracker.snapshot())
        with self.lock:
            self.last_snapshot = snap
            subs = list(self.subscribers)
        self._wake(subs, snap)

    def finish(self, state: str, result: dict | None = None,
               error: str | None = None) -> None:
        with self.lock:
            self.state = state
            self.result = result
            self.error = error
            subs = list(self.subscribers)
        # wake subscribers so their WS loop notices the terminal state
        self._wake(subs, self.last_snapshot or {})

    @staticmethod
    def _wake(subs: list[asyncio.Queue], item: dict) -> None:
        if _LOOP is None or _LOOP.is_closed():
            return

        def _put(q: asyncio.Queue) -> None:
            # coalesce: keep only the latest snapshot per slow subscriber
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

        for q in subs:
            try:
                _LOOP.call_soon_threadsafe(_put, q)
            except RuntimeError:
                return  # loop shut down mid-broadcast


REGISTRY: dict[str, RunHandle] = {}


def create(run_id: str) -> RunHandle:
    handle = RunHandle(id=run_id)
    handle.tracker = ProgressTracker(on_change=handle.push)
    REGISTRY[run_id] = handle
    return handle


def get(run_id: str) -> RunHandle | None:
    return REGISTRY.get(run_id)


def launch(handle: RunHandle, work) -> None:
    """Run `work(handle)` on a daemon thread; `work` must set the terminal
    state via handle.finish(...) itself (it knows done vs cancelled vs error)."""
    handle.thread = threading.Thread(target=work, args=(handle,), daemon=True)
    handle.thread.start()
