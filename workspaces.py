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
import time
import uuid
from pathlib import Path

from config import ROOT, WORKSPACES_SUBDIR, base_output_dir
from errors import ConfigError

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
    return {"id": wid, "name": name}
