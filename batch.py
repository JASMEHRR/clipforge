"""Batch queue: multiple files/URLs processed sequentially by a worker
thread. A failing job never kills the queue. Optional /inbox folder watcher
enqueues any video file dropped into it."""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from config import ROOT, load_config
from logutil import get_logger

log = get_logger("batch")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class JobQueue:
    """Sequential job queue with per-job status for the UI."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.items: list[dict] = []          # {id, source, status, message, job_dir}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self.watch_inbox = False

    # ------------------------------------------------------------- public

    def add(self, source: str, **options) -> str:
        item = {"id": uuid.uuid4().hex[:8], "source": str(source).strip(),
                "status": "queued", "message": "", "job_dir": "",
                "options": options}
        with self._lock:
            self.items.append(item)
        log.info("queued %s (%s)", item["source"], item["id"])
        self._ensure_worker()
        self._wake.set()
        return item["id"]

    def add_many(self, text: str, **options) -> int:
        n = 0
        for line in (text or "").splitlines():
            line = line.strip()
            if line:
                self.add(line, **options)
                n += 1
        return n

    def status_rows(self) -> list[list[str]]:
        with self._lock:
            return [[i["id"], i["source"][-60:], i["status"], i["message"]]
                    for i in self.items]

    def start_inbox_watcher(self) -> str:
        inbox = ROOT / self.cfg["paths"]["inbox_dir"]
        inbox.mkdir(exist_ok=True)
        (inbox / "processed").mkdir(exist_ok=True)
        self.watch_inbox = True
        if self._watcher is None or not self._watcher.is_alive():
            self._watcher = threading.Thread(target=self._watch_loop,
                                             daemon=True)
            self._watcher.start()
        return f"Watching {inbox} — drop video files there to enqueue them."

    def stop_inbox_watcher(self) -> str:
        self.watch_inbox = False
        return "Inbox watcher stopped."

    # ------------------------------------------------------------ threads

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work_loop, daemon=True)
            self._worker.start()

    def _next_queued(self) -> dict | None:
        with self._lock:
            return next((i for i in self.items if i["status"] == "queued"), None)

    def _work_loop(self) -> None:
        from pipeline import run_job
        while True:
            item = self._next_queued()
            if item is None:
                self._wake.clear()
                if not self._wake.wait(timeout=30):
                    continue
                continue
            item["status"] = "running"
            try:
                def cb(stage, frac, msg, _item=item):
                    _item["message"] = f"{int(frac * 100)}% {stage}"
                job = run_job(item["source"], self.cfg,
                              progress_cb=cb, **item["options"])
                kept = sum(1 for c in job["clips"] if c.get("kept"))
                item.update(status="done", job_dir=job["job_dir"],
                            message=f"{kept} clips kept")
            except Exception as e:  # noqa: BLE001 — queue must survive any job failure
                item.update(status="failed", message=str(e)[:200])
                log.error("batch job %s failed: %s — queue continues",
                          item["id"], e)

    def _watch_loop(self) -> None:
        inbox = ROOT / self.cfg["paths"]["inbox_dir"]
        processed = inbox / "processed"
        while True:
            if self.watch_inbox:
                for f in sorted(inbox.iterdir()):
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                        dest = processed / f"{int(time.time())}_{f.name}"
                        try:
                            f.rename(dest)   # move first: no partial reads
                            self.add(str(dest))
                        except OSError:
                            pass             # still being copied — next tick
            time.sleep(5)


QUEUE: JobQueue | None = None


def get_queue() -> JobQueue:
    global QUEUE
    if QUEUE is None:
        QUEUE = JobQueue()
    return QUEUE
