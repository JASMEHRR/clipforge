"""DEV-ONLY Phase 4 gate verification: every new screen and every picker
modal OPEN over real page content, at 1920x1080 and 1280x800.

Runtime: the real backend factory (server.create_app, what `run.bat new`
serves) on port 7871 with GEMINI_API_KEY forced empty (keyless). Uses the
newest existing job in output/ for the editor and results screens.

Run:  .venv\\Scripts\\python scripts\\screenshot_phase4.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from screenshot_phase3 import SIZES, URL, PORT, _both_sizes, _wait_ready  # noqa: E402


def newest_job() -> str:
    out = ROOT / "output"
    for d in sorted(out.iterdir(), reverse=True):
        if (d / "job.json").exists():
            job = json.loads((d / "job.json").read_text(encoding="utf-8"))
            if job.get("clips"):
                return d.name
    raise RuntimeError("no finished job with clips in output/")


def main() -> int:
    job = newest_job()
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

            def shot(base: str) -> None:
                _both_sizes(page, base)
                page.set_viewport_size({"width": 1920, "height": 1080})
                ok.append(base)

            def goto(route: str, wait: str) -> None:
                page.goto(URL + route, wait_until="load")
                page.wait_for_selector(wait, timeout=30000)
                page.evaluate("document.fonts.ready")
                page.wait_for_timeout(700)

            # home with both option panels open (pickers launch from here)
            goto("#/", ".hero .btn-primary")
            for s in page.locator(".opts > summary").all():
                s.click()
                page.wait_for_timeout(300)
            shot("p4_01_home_options")

            # picker modals — each opened over the real home screen
            pickers = [
                ("Caption style", "p4_02_picker_caption", ".pick img"),
                ("Caption font", "p4_03_picker_font", ".pick img"),
                ("Background music", "p4_04_picker_music", ".pick"),
                ("Watermark position", "p4_05_picker_position", ".pos-demo"),
                ("Clip shape", "p4_06_picker_shape", ".shape-demo"),
                ("If the video already has subtitles",
                 "p4_07_picker_subs", ".pick"),
                ("Editing style", "p4_08_picker_profile", ".pick"),
            ]
            for label, base, wait_sel in pickers:
                # find the picker button by its field label
                btn = page.locator(
                    f"xpath=//div[contains(@class,'field')]"
                    f"[label[contains(normalize-space(.), \"{label}\")]]"
                    f"//button[contains(@class,'picker-btn')]")
                btn.first.click()
                page.wait_for_selector(f"dialog[open] {wait_sel}", timeout=60000)
                page.wait_for_timeout(1000)
                shot(base)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

            # editor for the newest job's first clip
            goto(f"#/edit/{job}/0", ".editor-grid video")
            page.wait_for_timeout(1500)
            shot("p4_09_editor")

            # remaining screens
            goto("#/queue", ".queue-grid")
            shot("p4_10_queue")
            goto("#/youtube", ".yt-grid .card")
            shot("p4_11_youtube")
            goto("#/settings", ".settings-grid .card")
            shot("p4_12_settings")
            goto("#/history", ".history-grid .card, .history-grid .history-card")
            shot("p4_13_history")
            goto(f"#/results/{job}", ".clip-card video")
            page.wait_for_timeout(1500)
            shot("p4_14_results")

            browser.close()
    finally:
        server.terminate()
    print("captured:", ok)
    return 0 if len(ok) == 14 else 1


if __name__ == "__main__":
    raise SystemExit(main())
