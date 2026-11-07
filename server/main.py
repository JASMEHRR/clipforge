"""Entry point: `python -m server.main`. Serves the API + frontend and opens
the chromeless app window.

Port: CLIPFORGE_PORT env, default 7860.
"""
from __future__ import annotations

import os
import platform
from functools import wraps

import uvicorn

from config import load_config
from launcher import open_ui
from logutil import get_logger
from server import create_app

log = get_logger("server")


def _silence_windows_proactor_connection_reset() -> None:
    """Windows-only: asyncio's ProactorEventLoop logs a spurious
    ConnectionResetError/WinError 10054 traceback to the console whenever a
    client (browser tab, WebSocket, keep-alive) disconnects abruptly — this
    is a known upstream quirk (harmless, doesn't indicate anything actually
    broke), but it's noisy enough that it reads as a crash. Patches only the
    one method that raises it, and only swallows that one error class —
    every other exception still surfaces normally."""
    if platform.system() != "Windows":
        return
    from asyncio.proactor_events import _ProactorBasePipeTransport

    orig = _ProactorBasePipeTransport._call_connection_lost

    @wraps(orig)
    def _call_connection_lost(self, exc):
        try:
            orig(self, exc)
        except ConnectionResetError:
            pass

    _ProactorBasePipeTransport._call_connection_lost = _call_connection_lost


def main() -> None:
    _silence_windows_proactor_connection_reset()
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
