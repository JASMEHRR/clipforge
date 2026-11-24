"""Content loop: results -> brief -> videos -> post packages -> Muse -> results.

  .venv/Scripts/python.exe loop.py cycle [--dry-run] [--offline] [--make N]
  .venv/Scripts/python.exe loop.py ingest | plan | package | handoff | report | status

One cycle (nightly.bat runs it):
  1. ingest   YouTube results (ClipForge's analytics cache, refreshed when signed in)
              plus the Instagram and LinkedIn results Muse sends back.
  2. plan     learn what worked (loop_logic.learn), read today's trends (scout),
              write the brief: what to make next and why. Scout's picker reads it.
  3. make     scout makes N reels; every clip still waits for your approval.
  4. package  one post package per finished video: text and a time per platform.
  5. handoff  approved packages go to the folder Muse reads (loop.handoff_folder).
  6. report   the weekly note, and a status line for Jarvis.

Muse does the posting and returns results as JSON files (schema "loop_results")
into cache/loop/inbox or loop.results_folder. Runtime files live in cache/loop/
(gitignored). --dry-run makes no network calls, renders nothing and hands nothing
off; it still writes the brief, packages and report so the whole loop can be seen.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import loop_logic as L
from config import ROOT, base_output_dir, load_config
from logutil import get_logger
from schemas import SchemaValidationError, validate

log = get_logger("loop")

LOOP_DIR = ROOT / "cache" / "loop"
MAKE_TIMEOUT_S = 3 * 60 * 60


def _cfg(cfg: dict) -> dict:
    return cfg.get("loop") or {}


def _now() -> datetime:
    return datetime.now(L.IST)


def _write_json(path: Path, data) -> None:
    """Atomic write: a cut-off run never leaves half a file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("could not read %s (%s); using an empty value", path, e)
        return default


# ---------------------------------------------------------------- 1. ingest

def _youtube_rows(offline: bool) -> list[dict]:
    import analytics
    data = None
    if not offline:
        try:
            data = analytics.refresh(force=False)
        except Exception as e:  # not signed in, quota, network: fall back to the cache
            log.warning("YouTube analytics refresh failed (%s); using the cached copy", e)
    data = data or analytics.load_cache() or {}
    return L.from_youtube(data.get("videos") or [])


def _result_files(cfg: dict) -> list[Path]:
    folders = [LOOP_DIR / "inbox"]
    extra = str(_cfg(cfg).get("results_folder") or "").strip()
    if extra:
        folders.append(Path(extra))
    files = []
    for f in folders:
        if f.is_dir():
            files += sorted(f.glob("*.json"))
    return files


def ingest(cfg: dict, offline: bool) -> list[dict]:
    """Every result the loop can learn from. A results file that fails the
    schema is skipped and named in the log, never half-read."""
    rows = _youtube_rows(offline)
    for path in _result_files(cfg):
        data = _read_json(path, None)
        try:
            validate(data, "loop_results")
        except SchemaValidationError as e:
            log.warning("skipped results file %s: %s", path.name, e)
            continue
        rows += L.from_posts(data["posts"])
    _write_json(LOOP_DIR / "results.json", rows)
    log.info("ingest: %d results (%d YouTube)", len(rows), sum(r["platform"] == "youtube" for r in rows))
    return rows


# ---------------------------------------------------------------- 2. plan

def _trends(cfg: dict, offline: bool) -> list[dict]:
    cache = LOOP_DIR / "trends.json"
    if offline:
        return _read_json(cache, [])
    import scout
    try:
        lists = scout.fetch_candidates(cfg)
        cands = scout.rank(lists, scout.load_used(cfg), int((cfg.get("scout") or {}).get("shortlist", 30)))
    except Exception as e:  # every source down: keep yesterday's list rather than nothing
        log.warning("trend fetch failed (%s); using the last saved trends", e)
        return _read_json(cache, [])
    slim = [{"title": c.get("title", ""), "source": c.get("source", ""), "summary": c.get("summary", "")} for c in cands]
    _write_json(cache, slim)
    return slim


def plan(cfg: dict, rows: list[dict], offline: bool) -> dict:
    lc = _cfg(cfg)
    now = _now()
    signals = L.learn(rows, now, int(lc.get("lookback_days", 28)))
    brief = L.make_brief(signals, _trends(cfg, offline), now, int(lc.get("ideas", 3)))
    brief["signals"] = signals
    _write_json(LOOP_DIR / "brief.json", brief)
    note = L.prompt_note(brief) or "Not enough results yet: scout picks on its own."
    (LOOP_DIR / "brief.md").write_text(
        f"# Brief, {brief['made_at']}\n\n{note}\n\n" + "\n".join(f"- {i['title']}: {i['why']}" for i in brief["ideas"]) + "\n",
        encoding="utf-8")
    log.info("plan: %d ideas, learned=%s", len(brief["ideas"]), brief["enough_data"])
    return brief


# ---------------------------------------------------------------- 3. make

def make(n: int, dry: bool) -> int:
    """Scout makes n reels using the brief. Each clip waits for approval."""
    if dry or n <= 0:
        log.info("make: skipped (%s)", "dry run" if dry else "nothing asked")
        return 0
    try:
        done = subprocess.run([sys.executable, "scout.py", "--make", str(n)], cwd=ROOT, timeout=MAKE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.warning("make: scout ran past %d minutes and was stopped", MAKE_TIMEOUT_S // 60)
        return 1
    except OSError as e:
        log.warning("make: could not start scout (%s)", e)
        return 1
    log.info("make: scout finished with code %s", done.returncode)
    return done.returncode


# ---------------------------------------------------------------- 4. package

def _recent_clips(days: int) -> list[dict]:
    import upload_scheduler
    cutoff = datetime.now().timestamp() - days * 86400
    out = []
    for meta_path in base_output_dir().glob("**/clip_*/metadata.json"):
        clip_dir = meta_path.parent
        video = clip_dir / "final.mp4"
        if not video.exists() or video.stat().st_mtime < cutoff:
            continue
        meta = _read_json(meta_path, None)
        if not isinstance(meta, dict):
            continue
        social = _read_json(clip_dir / "social.json", {})
        out.append({
            "key": str(clip_dir.relative_to(ROOT)).replace("\\", "/"),
            "title": meta.get("title", ""), "description": meta.get("description", ""),
            "hashtags": meta.get("hashtags") or [], "video": str(video),
            "approval": upload_scheduler.approval_state(meta), "social": social if isinstance(social, dict) else {},
        })
    return out


def _outbox_posts() -> list[tuple[Path, dict]]:
    posts = []
    for p in (LOOP_DIR / "outbox").glob("*/post.json"):
        data = _read_json(p, None)
        if isinstance(data, dict):
            posts.append((p, data))
    return posts


def package(cfg: dict, brief: dict) -> list[dict]:
    """New packages for videos finished recently. A package is refreshed when the
    clip's approval changes, so an approved clip becomes ready for Muse."""
    days = int(_cfg(cfg).get("package_days", 7))
    existing = {d["key"]: (p, d) for p, d in _outbox_posts()}
    taken: dict[str, set[str]] = {}
    for _p, d in existing.values():
        for platform, body in (d.get("platforms") or {}).items():
            taken.setdefault(platform, set()).add(body.get("post_at", ""))
    made = []
    now = _now()
    for clip in _recent_clips(days):
        old = existing.get(clip["key"])
        if old:
            path, data = old
            if data.get("approval") != clip["approval"] and data.get("status") == "ready":
                data["approval"] = clip["approval"]
                _write_json(path, data)
            continue
        post = L.package(clip, brief, now, taken)
        try:
            validate(post, "loop_post")
        except SchemaValidationError as e:
            log.warning("package for %s failed its check (%s); skipped", clip["key"], e)
            continue
        _write_json(LOOP_DIR / "outbox" / post["id"] / "post.json", post)
        made.append(post)
    log.info("package: %d new packages", len(made))
    return made


# ---------------------------------------------------------------- 5. handoff

def handoff(cfg: dict, dry: bool) -> dict:
    """Approved packages go to the folder Muse reads. Pending ones wait: nothing is
    posted that you haven't approved in ClipForge."""
    folder = str(_cfg(cfg).get("handoff_folder") or "").strip()
    posts = _outbox_posts()
    ready = [(p, d) for p, d in posts if d.get("status") == "ready" and d.get("approval") == "approved"]
    waiting = sum(1 for _p, d in posts if d.get("status") == "ready" and d.get("approval") == "pending")
    result = {"handed_off": 0, "ready": len(ready), "waiting_approval": waiting, "folder": folder or None}
    if not folder:
        log.info("handoff: no loop.handoff_folder set; %d approved packages wait in %s", len(ready), LOOP_DIR / "outbox")
        return result
    if dry:
        log.info("handoff: dry run; would hand off %d packages to %s", len(ready), folder)
        return result
    for path, data in ready:
        dest = Path(folder) / data["id"]
        try:
            dest.mkdir(parents=True, exist_ok=True)
            video = Path(data["video"])
            if video.exists():
                shutil.copy2(video, dest / "video.mp4")
            data["status"] = "handed_off"
            data["handed_off_at"] = _now().isoformat(timespec="minutes")
            (dest / "post.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            _write_json(path, data)
            result["handed_off"] += 1
        except OSError as e:
            log.warning("handoff of %s failed (%s); it stays ready for the next run", data["id"], e)
    log.info("handoff: %d packages to %s", result["handed_off"], folder)
    return result


# ---------------------------------------------------------------- 6. report

def report(rows: list[dict], brief: dict, handed: dict) -> dict:
    """The weekly note, plus the status line the laptop bridge sends to Jarvis."""
    now = _now()
    week = L.weekly(rows, now)
    text = L.report_md(week, brief.get("signals") or {}, brief)
    (LOOP_DIR / "report.md").write_text(text, encoding="utf-8")
    needs = []
    if handed["waiting_approval"]:
        needs.append(f"{handed['waiting_approval']} new videos wait for your approval in ClipForge.")
    if not handed["folder"]:
        needs.append("Muse can't get the videos yet: set loop.handoff_folder to a folder Muse can read.")
    posted = sum(week[p]["this"]["posts"] for p in L.PLATFORMS)
    views = sum(week[p]["this"]["views"] for p in L.PLATFORMS)
    status = {
        "project": "clipforge", "label": "ClipForge",
        "ok": True, "summary": f"Loop ran: {len(brief.get('ideas', []))} ideas, {posted} posts and {views} views this week.",
        "needsYou": needs[:5], "lastRunAt": now.isoformat(timespec="minutes"),
        "numbers": {"postsThisWeek": posted, "viewsThisWeek": views, "waitingApproval": handed["waiting_approval"],
                    "readyForMuse": handed["ready"]},
    }
    _write_json(LOOP_DIR / "status.json", status)
    return status


# ---------------------------------------------------------------- CLI

def cycle(cfg: dict, dry: bool, offline: bool, n: int | None) -> int:
    rows = ingest(cfg, offline or dry)
    brief = plan(cfg, rows, offline or dry)
    code = make(int(_cfg(cfg).get("make_per_day", 2)) if n is None else n, dry)
    package(cfg, brief)
    handed = handoff(cfg, dry)
    status = report(rows, brief, handed)
    print(status["summary"])
    for item in status["needsYou"]:
        print(f"  needs you: {item}")
    print(f"brief: {LOOP_DIR / 'brief.md'}\nreport: {LOOP_DIR / 'report.md'}")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ClipForge content loop")
    ap.add_argument("command", choices=["cycle", "ingest", "plan", "package", "handoff", "report", "status"])
    ap.add_argument("--dry-run", action="store_true", help="no network, no rendering, no handoff")
    ap.add_argument("--offline", action="store_true", help="use cached analytics and trends")
    ap.add_argument("--make", type=int, default=None, help="reels to make this cycle (default loop.make_per_day)")
    a = ap.parse_args(argv)
    cfg = load_config()
    offline = a.offline or a.dry_run
    if a.command == "cycle":
        return cycle(cfg, a.dry_run, offline, a.make)
    if a.command == "ingest":
        print(f"{len(ingest(cfg, offline))} results")
        return 0
    if a.command == "plan":
        brief = plan(cfg, _read_json(LOOP_DIR / "results.json", []), offline)
        print((LOOP_DIR / "brief.md").read_text(encoding="utf-8"))
        return 0 if brief else 1
    if a.command == "package":
        print(f"{len(package(cfg, _read_json(LOOP_DIR / 'brief.json', {})))} new packages")
        return 0
    if a.command == "handoff":
        print(handoff(cfg, a.dry_run))
        return 0
    if a.command == "report":
        handed = handoff(cfg, True)
        report(_read_json(LOOP_DIR / "results.json", []), _read_json(LOOP_DIR / "brief.json", {}), handed)
        print((LOOP_DIR / "report.md").read_text(encoding="utf-8"))
        return 0
    status = _read_json(LOOP_DIR / "status.json", None)
    print(json.dumps(status, indent=2, ensure_ascii=False) if status else "The loop hasn't run yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
