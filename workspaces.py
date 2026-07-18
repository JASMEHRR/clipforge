"""Workspace (library) scoping: keeps each workspace's videos/clips in their
own output subfolder so they never mix. "default" is today's top-level
output/ dir (existing runs, untouched, no migration needed); every other
workspace gets output/_workspaces/<id>/.

config.current_workspace (a contextvar) holds the active workspace id for the
request; config.load_config() consults it and rewrites paths.output_dir
accordingly, so every existing caller of load_config()/output_root() is
scoped for free without touching pipeline.py, routes_library.py, bundle.py,
archive.py, etc.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

import yaml

from config import LOCAL_PATH, ROOT, WORKSPACES_SUBDIR, base_output_dir, save_config
from errors import ConfigError
from logutil import get_logger

log = get_logger("server")
WORKSPACES_JSON = ROOT / "cache" / "workspaces.json"


def _load() -> dict:
    if not WORKSPACES_JSON.exists():
        return {"workspaces": {}}
    try:
        return json.loads(WORKSPACES_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"workspaces": {}}


def _save(data: dict) -> None:
    WORKSPACES_JSON.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                encoding="utf-8")


def output_dir_for(workspace_id: str) -> Path:
    base = base_output_dir()
    if workspace_id == "default":
        return base
    return base / WORKSPACES_SUBDIR / workspace_id


def list_workspaces() -> list[dict]:
    """"default" always exists, even with no entry on disk (it's today's
    output/ folder, not something that needs creating)."""
    data = _load()
    out = [{"id": "default", "name": "Default"}]
    for wid, w in data.get("workspaces", {}).items():
        out.append({"id": wid, "name": w.get("name", wid),
                    "created_at": w.get("created_at")})
    return out


def create_workspace(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ConfigError("Workspace name can't be empty.")
    data = _load()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    wid = slug
    existing_ids = set(data.setdefault("workspaces", {})) | {"default"}
    if wid in existing_ids:
        wid = f"{slug}-{uuid.uuid4().hex[:4]}"
    data["workspaces"][wid] = {"name": name, "created_at": time.time()}
    _save(data)
    output_dir_for(wid).mkdir(parents=True, exist_ok=True)
    # each workspace is its own YouTube destination account (same id) — see
    # server/routes_upload.py's _account(), which maps the active workspace
    # straight onto upload.accounts.<id>. Keep 'default' present too — an
    # accounts dict with only the new entry would make list_accounts() drop
    # 'default' entirely (it only falls back to ['default'] when the dict is
    # empty).
    save_config({"upload": {"accounts": {"default": {}, wid: {}}}})
    return {"id": wid, "name": name}


def delete_workspace(workspace_id: str) -> None:
    """Deletes the workspace's videos/clips, its YouTube account config and
    cached OAuth token, and re-homes anything still pointing at it (queued
    clips, pulled channels) back onto "default" so nothing is stranded."""
    if workspace_id == "default":
        raise ConfigError("The Default workspace can't be deleted.")
    data = _load()
    if workspace_id not in data.get("workspaces", {}):
        raise ConfigError("That workspace can't be found.")

    shutil.rmtree(output_dir_for(workspace_id), ignore_errors=True)

    if LOCAL_PATH.exists():
        local = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {}
        accts = (local.get("upload") or {}).get("accounts") or {}
        if workspace_id in accts:
            del accts[workspace_id]
            LOCAL_PATH.write_text(yaml.safe_dump(local, sort_keys=False),
                                  encoding="utf-8")
            import config
            config.reload_config()

    import channels
    from upload_scheduler import load_queue, save_queue
    channels.reassign_account(workspace_id, "default")
    qdata = load_queue()
    moved = False
    for e in qdata["queue"]:
        if e.get("account", "default") == workspace_id:
            e["account"] = "default"
            moved = True
    if moved:
        save_queue(qdata)

    import youtube_upload as yt
    try:
        tok = yt.token_path(workspace_id)
        if tok.exists():
            tok.unlink()
    except OSError as e:  # noqa: BLE001 — token cleanup is best-effort
        log.warning("could not remove token for workspace %s: %s", workspace_id, e)

    del data["workspaces"][workspace_id]
    _save(data)
