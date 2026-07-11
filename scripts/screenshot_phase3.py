"""DEV-ONLY Phase 3 gate verification: drive a full keyless run through the
NEW frontend (submit -> progress -> results) and screenshot every screen at
1920x1080 and 1280x800.

Runtime: the real backend entry (server.create_app, the same factory
`run.bat new` serves) on port 7871, with GEMINI_API_KEY forced empty so the
'auto' provider resolves to mock (keyless). Playwright Chromium windows at
both target sizes stand in for the chromeless app window.

Not part of the app or requirements.txt (playwright dev install, see
screenshot_ui.py). Run:  .venv\\Scripts\\python scripts\\screenshot_phase3.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 7871
URL = f"http://127.0.0.1:{PORT}/"
OUT = ROOT / "design" / "screenshots"
SIZES = {"1920": (1920, 1080), "1280": (1280, 800)}


def _wait_ready(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(URL + "api/presets", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("server never became ready")


def _shot(page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / name), full_page=True)
    print("saved", name)


def _both_sizes(page, base: str, settle_ms: int = 400) -> None:
    for tag, (w, h) in SIZES.items():
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(settle_ms)
        _shot(page, f"{base}_{tag}.png")


def main() -> int:
    env = dict(os.environ, GEMINI_API_KEY="", CLIPFORGE_PORT=str(PORT))
    server = subprocess.Popen(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c",
         "import uvicorn; from server import create_app; "
         f"uvicorn.run(create_app(), host='127.0.0.1', port={PORT}, "
         "log_level='warning')"],
        cwd=str(ROOT), env=env)
    ok = []
    try:
        _wait_ready()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(URL, wait_until="load")
            page.wait_for_selector(".hero .btn-primary", timeout=15000)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(800)

            # 1. home, hero only, then options open
            _both_sizes(page, "p3_01_home")
            ok.append("home")
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.locator(".opts > summary").click()
            page.wait_for_timeout(600)          # selects populate async
            _both_sizes(page, "p3_02_home_options")
            ok.append("home_options")

            # 2. submit the bundled sample by path (keyless mock run)
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.get_by_placeholder("Paste a YouTube or video link").fill(
                str(ROOT / "samples" / "sample.mp4"))
            page.get_by_role("button", name="Make clips").click()
            page.wait_for_url("**/#/run/**", timeout=15000)

            # 3. progress mid-run: wait until the board shows real progress
            page.wait_for_function(
                "() => Number(document.querySelector('.t-hero-num')"
                "?.textContent || 0) >= 5", timeout=300000)
            _both_sizes(page, "p3_03_progress")
            ok.append("progress")

            # 4. results after the run completes (mock render is minutes)
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.wait_for_url("**/#/results/**", timeout=1200000)
            page.wait_for_selector(".clip-card video", timeout=60000)
            page.wait_for_timeout(3000)         # video first frames
            _both_sizes(page, "p3_04_results", settle_ms=1200)
            ok.append("results")

            browser.close()
    finally:
        server.terminate()
    print("captured:", ok)
    return 0 if len(ok) == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
