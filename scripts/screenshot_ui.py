"""DEV-ONLY visual verification: launch the ClipForge UI in-process and capture
real screenshots of each tab + the font-gallery popup + the card Edit flow.

Not part of the app or requirements.txt. Requires a one-off dev install:
    .venv\\Scripts\\python -m pip install playwright
    .venv\\Scripts\\python -m playwright install chromium
Run:  .venv\\Scripts\\python scripts\\screenshot_ui.py
Screenshots land in design/screenshots/. Each step is guarded so a failure in
one (e.g. a slow pipeline run) still yields the other shots."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gradio as gr  # noqa: E402

import app  # noqa: E402

PORT = 7870
URL = f"http://127.0.0.1:{PORT}/"
OUT = app.ROOT / "design" / "screenshots"


def _shot(page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / name), full_page=True)
    print("saved", name)


def _click_tab(page, label: str) -> None:
    page.get_by_role("tab", name=label).click()
    page.wait_for_timeout(700)


def main() -> int:
    from playwright.sync_api import sync_playwright

    demo = app.build_app().queue()
    demo.launch(server_name="127.0.0.1", server_port=PORT, inbrowser=False,
                quiet=True, prevent_thread_lock=True,
                theme=gr.themes.Soft(primary_hue="violet"), css=app.APP_CSS,
                allowed_paths=[str(app.ROOT / "cache" / "font_previews"),
                               str(app.ROOT / "assets")])
    time.sleep(2)
    ok = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1600})
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(2500)

        try:                                    # 1. Create tab (themed, empty)
            _shot(page, "01_create.png")
            ok.append("create")
        except Exception as e:
            print("create shot failed:", e)

        try:                                    # 2. font gallery popup (real previews)
            page.get_by_text("Style & Branding", exact=True).click()
            page.wait_for_timeout(500)
            page.get_by_role("button", name="Browse fonts").click()
            page.wait_for_selector(".cf-font-row img", timeout=20000)
            page.wait_for_timeout(1200)
            _shot(page, "02_font_gallery.png")
            ok.append("font_gallery")
            page.get_by_role("button", name="Close").click()
            page.wait_for_timeout(500)
        except Exception as e:
            print("font gallery shot failed:", e)

        try:                                    # 3. History tab + reopen a job
            _click_tab(page, "History")
            _shot(page, "03_history.png")
            jobs = app._list_job_dirs()
            if jobs:
                page.get_by_label("Job id to reopen").fill(jobs[0])
                page.get_by_role("button", name="Open job").click()
                page.wait_for_timeout(2500)
                _shot(page, "04_history_reopened.png")
            ok.append("history")
        except Exception as e:
            print("history shot failed:", e)

        try:                                    # 4. run a mock job → card Edit flow
            _click_tab(page, "Create")
            page.locator("input[type=file]").first.set_input_files(
                str(app.ROOT / "samples" / "sample.mp4"))
            page.wait_for_timeout(2500)         # let the upload settle
            page.get_by_role("button", name="Create clips").click()
            # wait (generously) for the first card's Edit button to appear
            page.wait_for_selector("text=Edit this clip", timeout=600000)
            page.wait_for_timeout(1500)
            _shot(page, "05_create_results.png")
            ok.append("create_results")
            page.get_by_role("button", name="Edit this clip").first.click()
            page.wait_for_timeout(2500)
            _shot(page, "06_edit_loaded.png")
            ok.append("edit_loaded")
        except Exception as e:
            print("run/edit shot failed:", e)

        browser.close()
    demo.close()
    print("captured:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
