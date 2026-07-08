"""SQLite job history: every job with source, settings, clip list, timings.
Backs the UI History tab (reopen past results, re-download clips)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from config import ROOT, load_config
from logutil import get_logger

log = get_logger("history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id   TEXT PRIMARY KEY,
    created  TEXT NOT NULL,
    source   TEXT NOT NULL,
    status   TEXT NOT NULL,
    job_dir  TEXT NOT NULL,
    settings TEXT NOT NULL,   -- JSON
    stages   TEXT NOT NULL,   -- JSON timings
    clips    TEXT NOT NULL,   -- JSON clip list
    notes    TEXT NOT NULL    -- JSON list
);
"""


def _db(cfg: dict | None = None) -> sqlite3.Connection:
    cfg = cfg or load_config()
    conn = sqlite3.connect(ROOT / cfg["paths"]["db_path"], timeout=15)
    conn.execute(_SCHEMA)
    return conn


def record_job(job: dict, job_dir: str | Path, cfg: dict | None = None) -> None:
    """Insert or update a finished job. Failures here never break the run."""
    try:
        with _db(cfg) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (job["job_id"], job["created"], job["source"], job["status"],
                 str(job_dir), json.dumps(job["settings"]),
                 json.dumps(job["stages"]), json.dumps(job["clips"]),
                 json.dumps(job.get("notes", []))))
        log.info("history: recorded job %s (%s)", job["job_id"], job["status"])
    except Exception as e:  # noqa: BLE001 — history is best-effort
        log.error("history write failed (non-fatal): %s", e)


def list_jobs(cfg: dict | None = None, limit: int = 100) -> list[dict]:
    try:
        with _db(cfg) as conn:
            rows = conn.execute(
                "SELECT job_id, created, source, status, job_dir, clips "
                "FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    except Exception as e:  # noqa: BLE001
        log.error("history read failed: %s", e)
        return []
    out = []
    for job_id, created, source, status, job_dir, clips in rows:
        clist = json.loads(clips)
        out.append({"job_id": job_id, "created": created, "source": source,
                    "status": status, "job_dir": job_dir,
                    "clip_count": len(clist),
                    "kept": sum(1 for c in clist if c.get("kept"))})
    return out


def render_rate_history(cfg: dict | None = None, limit: int = 10) -> list[float]:
    """Past render throughput as output-seconds rendered per wall-clock second,
    oldest→newest, for ETA estimation. Prefers per-clip ``render_s`` when present,
    else the aggregate ``render_clips`` stage timing. Best-effort — [] on error."""
    try:
        with _db(cfg) as conn:
            rows = conn.execute(
                "SELECT stages, clips FROM jobs WHERE status='done' "
                "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    except Exception as e:  # noqa: BLE001
        log.error("history rate read failed: %s", e)
        return []
    rates = []
    for stages_json, clips_json in rows:
        try:
            stages = json.loads(stages_json)
            clips = json.loads(clips_json)
            out_secs = sum(float(c.get("duration", 0) or 0) for c in clips)
            wall = sum(float(c.get("render_s", 0) or 0) for c in clips)
            if wall <= 0:  # fall back to the aggregate render stage timing
                st = stages.get("render_clips") or stages.get("render") or {}
                wall = float(st.get("seconds", 0) or 0)
            if out_secs > 0 and wall > 0:
                rates.append(out_secs / wall)
        except Exception:  # noqa: BLE001 — skip malformed rows
            continue
    rates.reverse()  # oldest→newest for EMA
    return rates


def get_job(job_id: str, cfg: dict | None = None) -> dict | None:
    with _db(cfg) as conn:
        row = conn.execute("SELECT job_id, created, source, status, job_dir, "
                           "settings, stages, clips, notes FROM jobs WHERE "
                           "job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return {"job_id": row[0], "created": row[1], "source": row[2],
            "status": row[3], "job_dir": row[4],
            "settings": json.loads(row[5]), "stages": json.loads(row[6]),
            "clips": json.loads(row[7]), "notes": json.loads(row[8])}
