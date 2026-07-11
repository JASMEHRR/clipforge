"""DEV-ONLY screenshot proof for the Upload-now batch feature.

Runs the real server IN-PROCESS (not a subprocess) so this script can
monkeypatch youtube_upload.build_service/upload_clip before the app starts —
the confirm step's actual upload call is faked, so clicking Confirm in this
run can NEVER publish a real video. Everything else is genuine: real
authorization state, real candidate clips from output/ (found via the real
upload_scheduler.find_candidates), real scoring/eligibility. The upload log
is redirected to a scratch file so no fake video_id ever lands in the real
cache/upload_log.json.

Run:  .venv\\Scripts\\python scripts\\screenshot_upload_queue.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 7872
URL = f"http://127.0.0.1:{PORT}/"
OUT = ROOT / "design" / "screenshots"
SIZES = {"1920": (1920, 1080), "1280": (1280, 800)}


def _shot(page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / name), full_page=True)
    print("saved", name)


def _both_sizes(page, base: str, settle_ms: int = 500) -> None:
    for tag, (w, h) in SIZES.items():
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(settle_ms)
        _shot(page, f"{base}_{tag}.png")


def main() -> int:
    import youtube_upload as yt
    import upload_scheduler as sched

    scratch_log = ROOT / "cache" / "upload_log.screenshot_scratch.json"
    # seed a couple of already-uploaded entries so the UPLOADED section shows;
    # keys are fake so they don't exclude any real candidate from the queue
    scratch_log.write_text(json.dumps({"uploads": {
        "_demo/uploaded_a": {"video_id": "dEmoVid001", "title": "Best moment so far",
                             "uploaded_at": "2026-07-11T18:30:00+05:30",
                             "publish_at": "2026-07-11T18:30:00+05:30",
                             "virality_score": 88},
        "_demo/uploaded_b": {"video_id": "dEmoVid002", "title": "The plot twist",
                             "uploaded_at": "2026-07-10T12:05:00+05:30",
                             "publish_at": "2026-07-10T12:05:00+05:30",
                             "virality_score": 74},
    }}), encoding="utf-8")
    sched.LOG_FILE = scratch_log   # real candidates, but a throwaway log

    # enable the upload-time end watermark so the confirm step shows the
    # end-card preview (config is cached; force it before the app starts)
    import config as config_mod
    cfg = config_mod.load_config()
    cfg["upload"]["end_watermark"] = {"enabled": True, "text": "ClipForge",
                                      "duration_s": 1.2}
    config_mod._cached = cfg

    def fake_build_service(service=None):
        return object()

    def fake_upload_clip(video_path, metadata, privacy="private", service=None,
                         publish_at=None, category_id=None):
        assert privacy == "public" and publish_at is None
        time.sleep(0.4)   # feel like real network latency in the progress UI
        return {"video_id": "SCREENSHOT_FAKE", "url": "https://youtu.be/SCREENSHOT_FAKE"}

    yt.build_service = fake_build_service
    yt.upload_clip = fake_upload_clip

    import uvicorn
    from server import create_app
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=PORT,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.1)

        ok = []
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(URL + "#/youtube", wait_until="load")
            page.wait_for_selector(".uq-controls", timeout=15000)
            page.wait_for_timeout(800)

            # 1. the count selector + candidate list, real data
            _both_sizes(page, "uq_01_queue")
            ok.append("queue")

            # 1b. click a thumbnail -> full click-to-play preview modal
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.locator(".uq-thumb-btn").first.click()
            page.wait_for_selector("dialog.dialog-preview[open] .clip-preview-video",
                                   timeout=15000)
            page.wait_for_timeout(700)
            _both_sizes(page, "uq_01b_preview")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            ok.append("preview")

            # 2. confirm dialog open (top-N path)
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.locator(".uq-controls input[type=number]").fill("3")
            page.get_by_role("button", name="Upload now").click()
            page.wait_for_selector("dialog[open] .uq-confirm-row", timeout=15000)
            page.wait_for_timeout(700)
            _both_sizes(page, "uq_02_confirm")
            ok.append("confirm")

            # 3. confirm -> watch it run -> completed summary
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.get_by_role("button", name="Confirm — upload now").click()
            page.wait_for_selector("dialog[open] .badge-ok, dialog[open] .badge-warn",
                                   timeout=20000)
            # batch is finished once the summary reports the tally
            page.wait_for_selector(
                "dialog[open] .dialog-body p:has-text('uploaded')", timeout=20000)
            page.wait_for_timeout(600)
            _both_sizes(page, "uq_03_result")
            ok.append("result")

            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        scratch_log.unlink(missing_ok=True)
    print("captured:", ok)
    return 0 if len(ok) == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
