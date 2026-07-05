"""Config loading: config.yaml + .env (optional). Also the Python-version
startup check and config hashing for cache keys."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

from errors import ConfigError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

_cached: dict | None = None


def check_python_version(cfg: dict) -> None:
    required = cfg.get("python", {}).get("required", "3.11")
    major, minor = (int(x) for x in required.split(".")[:2])
    if sys.version_info[:2] != (major, minor):
        if cfg.get("python", {}).get("allow_unsupported"):
            return
        raise ConfigError(
            f"Python {required}.x is required (you are on "
            f"{sys.version_info.major}.{sys.version_info.minor}). "
            "MediaPipe wheels lag newer Python versions. Install Python "
            f"{required} and recreate the venv, or set python.allow_unsupported: "
            "true in config.yaml at your own risk."
        )


def load_config(path: Path | None = None, check_python: bool = True) -> dict:
    """Load config.yaml, overlay .env into os.environ (never into the dict)."""
    global _cached
    if _cached is not None and path is None:
        return _cached
    p = path or CONFIG_PATH
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if check_python:
        check_python_version(cfg)
    _load_dotenv(ROOT / ".env")
    if path is None:
        _cached = cfg
    return cfg


def _load_dotenv(env_path: Path) -> None:
    """Minimal .env loader (no dependency needed at import time). Existing
    environment variables win; the build never requires .env to exist."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def config_hash(cfg: dict, *sections: str) -> str:
    """Stable short hash of selected config sections (cache keys)."""
    subset = {s: cfg.get(s) for s in sections} if sections else cfg
    blob = json.dumps(subset, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def file_hash(path: str | Path, chunk_mb: int = 8) -> str:
    """Streaming sha256 of a file (memory-safe for large videos)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_mb * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()
