"""Entry point: `python -m server.main`. Serves the API + frontend and opens
the chromeless app window.

Port: CLIPFORGE_PORT env, default 7860.
"""
from __future__ import annotations

import os

import uvicorn

from config import load_config
from launcher import open_ui
from logutil import get_logger
from server import create_app

log = get_logger("server")


def main() -> None:
    port = int(os.environ.get("CLIPFORGE_PORT", "7860"))
    host = os.environ.get("CLIPFORGE_HOST", "127.0.0.1")
    cfg = load_config()
    if host == "127.0.0.1":
        # opens once the server answers HTTP (launcher polls readiness)
        open_ui(f"http://127.0.0.1:{port}", cfg)
    log.info("ClipForge UI on http://%s:%d", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
