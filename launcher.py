"""Auto-open the ClipForge UI when the server starts.

``window_mode: app`` launches a Chromium browser (Edge/Chrome) with ``--app=URL``
so the UI gets its own chromeless window; ``tab`` (or no Chromium found) falls
back to a normal browser tab via the stdlib ``webbrowser`` module. The
command-construction logic is pure so it can be unit-tested without ever opening
a browser.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from logutil import get_logger

log = get_logger("launcher")

# Known Chromium install locations by platform (checked in order).
_WIN_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
_MAC_CANDIDATES = [
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
_LINUX_NAMES = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"]


def detect_browser() -> str | None:
    """Absolute path to a Chromium-family browser, or None if none is found."""
    if sys.platform.startswith("win"):
        candidates = _WIN_CANDIDATES
    elif sys.platform == "darwin":
        candidates = _MAC_CANDIDATES
    else:
        candidates = []
        for name in _LINUX_NAMES:
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def build_launch_command(url: str, window_mode: str = "app",
                         browser_path: str | None = None) -> list[str] | None:
    """The argv to open ``url``, or None to signal 'use the webbrowser fallback'.

    Pure — takes an explicit ``browser_path`` so tests never touch the machine.
    Returns None when window_mode != 'app' or no Chromium browser is available.
    """
    if window_mode != "app" or not browser_path:
        return None
    return [browser_path, f"--app={url}", "--new-window"]


def _wait_until_up(url: str, timeout: float = 20.0) -> bool:
    """Poll the server until it answers or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status < 500:
                    return True
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.4)
    return False


def open_ui(url: str, cfg: dict, block: bool = False) -> None:
    """Open the UI per config once the server is reachable. Best-effort: any
    failure just logs and leaves the server running (protocol: never crash)."""
    ui = (cfg or {}).get("ui", {})
    if not ui.get("auto_open", True):
        return
    window_mode = ui.get("window_mode", "app")

    def _go():
        if not _wait_until_up(url):
            log.warning("auto-open: server not reachable at %s — skipping", url)
            return
        import subprocess
        # Cache-buster: a unique query per launch makes the browser re-fetch
        # index.html (its URL changed), which then revalidates app.js and its
        # module imports against the server's no-cache headers — so a fresh
        # start always shows the latest UI without any manual hard-refresh.
        sep = "&" if "?" in url else "?"
        open_url = f"{url}{sep}v={int(time.time())}"
        cmd = build_launch_command(open_url, window_mode, detect_browser())
        try:
            if cmd:
                log.info("auto-open: launching app window: %s", " ".join(cmd))
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                log.info("auto-open: opening browser tab at %s", open_url)
                webbrowser.open(open_url)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-open failed (non-fatal): %s", e)

    if block:
        _go()
    else:
        threading.Thread(target=_go, daemon=True).start()


if __name__ == "__main__":
    # self-check: pure command construction, no browser opened
    cmd = build_launch_command("http://x", "app", "/path/to/chrome")
    assert cmd == ["/path/to/chrome", "--app=http://x", "--new-window"], cmd
    assert build_launch_command("http://x", "tab", "/path/to/chrome") is None
    assert build_launch_command("http://x", "app", None) is None
    print("ok:", os.name)
