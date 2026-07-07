"""Self-update system.

On launch, a background thread checks GitHub for a newer version (non-blocking,
silent on any network failure). The UI shows a banner with a one-click
"Install update" button. Applying an update:

  1. Determines changed files via the GitHub compare API (delta update);
     falls back to the full release zipball if the compare fails.
  2. Downloads with resume support into cache/updates/ (integrity-checked:
     zip CRC test / per-file blob sha match).
  3. Verifies the staged code (every .py must byte-compile; key files present).
  4. Backs up the current code files to cache/updates/backup_<version>/.
  5. Replaces ONLY application code files. User data is never touched:
     output/, cache/, samples/, inbox/, .env, jobs.db, tools/ are preserved.
     A locally modified config.yaml is kept; the new one lands as
     config.yaml.new for manual review.
  6. Rolls back automatically from the backup if anything fails mid-apply.

The running process keeps its old code in memory — the user is told to
restart; a running job is never interrupted.
"""
from __future__ import annotations

import hashlib
import io
import json
import py_compile
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from logutil import get_logger

log = get_logger("updater")

ROOT = Path(__file__).resolve().parent
REPO = "JASMEHRR/clipforge"
API = f"https://api.github.com/repos/{REPO}"
UPDATES_DIR = ROOT / "cache" / "updates"

# Paths the updater may write to. Everything else (user data) is untouchable.
CODE_GLOBS = ("*.py", "*.md", "*.txt", "*.yml", "*.yaml", "*.sh", "*.bat",
              "VERSION", "LICENSE", ".env.example")
CODE_DIRS = ("scripts", "tests", "deploy", "assets")
PRESERVE_ALWAYS = {"output", "cache", "samples", "inbox", "tools",
                   ".venv", ".git", ".env", "jobs.db", "config.yaml"}

_state_lock = threading.Lock()
_state: dict = {"checked": False, "update_available": False,
                "current": "", "latest": "", "notes": "", "error": ""}


def current_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _semver(v: str) -> tuple:
    v = v.lstrip("v").split("-")[0]
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _get_json(url: str, timeout: int = 8):
    import os

    import requests
    headers = {"User-Agent": "ClipForge-updater",
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("GitHub rate limit hit — will retry later")
    r.raise_for_status()
    return r.json()


def _latest_release() -> dict | None:
    """Newest version on GitHub: releases first, tags as fallback."""
    try:
        rel = _get_json(f"{API}/releases/latest")
        return {"version": rel["tag_name"], "notes": rel.get("body", "") or "",
                "zip_url": rel["zipball_url"]}
    except Exception:  # noqa: BLE001 — releases may not exist; try tags
        pass
    try:
        tags = _get_json(f"{API}/tags")
        if not tags:
            return None
        best = max(tags, key=lambda t: _semver(t["name"]))
        return {"version": best["name"], "notes": "",
                "zip_url": best["zipball_url"]}
    except Exception:  # noqa: BLE001 — offline is normal; never crash launch
        return None


def check_for_update() -> dict:
    """Synchronous check; safe to call anytime. Returns the state dict."""
    cur = current_version()
    latest = _latest_release()
    with _state_lock:
        _state.update(checked=True, current=cur)
        if latest is None:
            _state["error"] = "could not reach GitHub (offline?)"
            return dict(_state)
        _state["error"] = ""
        _state["latest"] = latest["version"]
        _state["notes"] = latest["notes"][:2000]
        _state["zip_url"] = latest["zip_url"]
        _state["update_available"] = _semver(latest["version"]) > _semver(cur)
    if _state["update_available"]:
        log.info("update available: %s -> %s", cur, latest["version"])
    return dict(_state)


def check_async() -> None:
    """Fire-and-forget launch-time check (never blocks or raises)."""
    threading.Thread(target=lambda: _safe(check_for_update),
                     daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log.warning("update check failed: %s", e)


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


# ------------------------------------------------------------ delta fetch

def _changed_files(cur: str, latest: str) -> list[dict] | None:
    """[{path, sha, status}] from the compare API, or None → full update."""
    try:
        cur_tag = cur if cur.startswith("v") else f"v{cur}"
        cmp = _get_json(f"{API}/compare/{cur_tag}...{latest}", timeout=15)
        files = cmp.get("files", [])
        if not files or len(files) > 300:
            return None
        return [{"path": f["filename"], "status": f["status"],
                 "sha": f.get("sha", "")} for f in files]
    except Exception:  # noqa: BLE001 — compare needs both tags to exist
        return None


def _download_resumable(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "ClipForge-updater"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        mode = "ab" if have and r.status == 206 else "wb"
        with open(part, mode) as f:
            shutil.copyfileobj(r, f, length=1 << 20)
    part.replace(dest)
    return dest


def _blob_sha(data: bytes) -> str:
    """git blob sha1 for per-file integrity verification."""
    h = hashlib.sha1(f"blob {len(data)}\0".encode())
    h.update(data)
    return h.hexdigest()


def _stage_delta(files: list[dict], latest: str, staging: Path) -> list[dict]:
    """Download only changed files into staging; verify each blob sha."""
    kept = []
    for f in files:
        if f["status"] == "removed":
            kept.append(f)
            continue
        url = (f"https://raw.githubusercontent.com/{REPO}/{latest}/"
               f"{f['path']}")
        req = urllib.request.Request(url, headers={"User-Agent": "ClipForge"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if f["sha"] and _blob_sha(data) != f["sha"]:
            raise RuntimeError(f"integrity check failed for {f['path']}")
        out = staging / f["path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        kept.append(f)
    return kept


def _stage_full(zip_url: str, latest: str, staging: Path) -> None:
    """Full zipball fallback: download (resumable), verify, extract."""
    zip_path = _download_resumable(zip_url,
                                   UPDATES_DIR / f"clipforge-{latest}.zip")
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            zip_path.unlink(missing_ok=True)
            raise RuntimeError("downloaded update archive is corrupted — "
                               "try again")
        rootdir = z.namelist()[0].split("/")[0]
        for name in z.namelist():
            rel = name[len(rootdir) + 1:]
            if not rel or name.endswith("/"):
                continue
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(name))


# ------------------------------------------------------------------ apply

def _is_updatable(rel: str) -> bool:
    top = rel.split("/")[0]
    if top in PRESERVE_ALWAYS:
        return False
    return True


def _verify_staged(staging: Path, full: bool) -> None:
    """Never install something broken: every .py must compile; a full update
    must contain the application's key files."""
    for py in staging.rglob("*.py"):
        py_compile.compile(str(py), cfile=str(py) + "c", doraise=True)
        Path(str(py) + "c").unlink(missing_ok=True)
    if full:
        for required in ("app.py", "pipeline.py", "VERSION"):
            if not (staging / required).exists():
                raise RuntimeError(f"staged update is incomplete "
                                   f"(missing {required})")


def _backup_current(files: list[Path], version: str) -> Path:
    backup = UPDATES_DIR / f"backup_{version}"
    if backup.exists():
        shutil.rmtree(backup)
    for f in files:
        rel = f.relative_to(ROOT)
        dest = backup / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if f.exists():
            shutil.copy2(f, dest)
    return backup


def _rollback(backup: Path) -> None:
    for f in backup.rglob("*"):
        if f.is_file():
            dest = ROOT / f.relative_to(backup)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)


def apply_update() -> str:
    """One-click update. Returns a human-readable result message."""
    state = get_state() if get_state()["checked"] else check_for_update()
    if not state.get("update_available"):
        return f"Already up to date (v{current_version()})."
    latest, cur = state["latest"], state["current"]
    staging = UPDATES_DIR / f"staging_{latest}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    # 1. stage: delta when possible, full zipball otherwise
    files = _changed_files(cur, latest)
    delta = files is not None
    try:
        if delta:
            log.info("delta update: %d changed files", len(files))
            files = _stage_delta(files, latest, staging)
        else:
            log.info("full update: downloading release archive")
            _stage_full(state["zip_url"], latest, staging)
    except Exception as e:  # noqa: BLE001
        if delta:
            log.warning("delta update failed (%s) — falling back to full", e)
            _stage_full(state["zip_url"], latest, staging)
            delta, files = False, None
        else:
            raise

    _verify_staged(staging, full=not delta)

    # 2. compute target files, filter protected paths, handle config.yaml
    staged = [p for p in staging.rglob("*") if p.is_file()]
    targets: list[tuple[Path, Path]] = []
    for p in staged:
        rel = str(p.relative_to(staging)).replace("\\", "/")
        if not _is_updatable(rel):
            if rel == "config.yaml":
                shutil.copy2(p, ROOT / "config.yaml.new")
                log.info("config.yaml preserved; new version saved as "
                         "config.yaml.new")
            continue
        targets.append((p, ROOT / rel))
    removals = [ROOT / f["path"] for f in (files or [])
                if f["status"] == "removed" and _is_updatable(f["path"])]

    # 3. backup, then apply with automatic rollback
    backup = _backup_current([t for _, t in targets] + removals, cur)
    try:
        for src, dest in targets:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        for r in removals:
            r.unlink(missing_ok=True)
        (ROOT / "VERSION").write_text(latest.lstrip("v") + "\n",
                                      encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — restore everything on any failure
        log.error("update failed mid-apply (%s) — rolling back", e)
        _rollback(backup)
        raise RuntimeError(f"update failed and was rolled back: {e}") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    with _state_lock:
        _state["update_available"] = False
        _state["current"] = latest.lstrip("v")
    kind = "delta" if delta else "full"
    log.info("updated %s -> %s (%s)", cur, latest, kind)
    return (f"Updated to {latest} ({kind} update, settings/models/videos "
            f"preserved, backup kept in cache/updates/). "
            f"Restart ClipForge to use the new version — the running app "
            f"keeps working until then.")
