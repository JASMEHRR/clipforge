"""DEV-ONLY screenshot proof for the Analytics tab.

Runs the real server in-process (not a subprocess) so this script can fake
youtube_upload.build_analytics_service before the app starts — the actual
Google Analytics call is faked, so this run never touches a real channel.
Everything else is genuine: the real /api/analytics/state route, the real
analytics.py fetch/cache/join path, analytics_insights.recommend() run on
the synthetic data, and the real frontend rendering it. The analytics cache
and upload log are redirected to scratch files so nothing lands in the real
cache/.

Run:  .venv\\Scripts\\python scripts\\screenshot_analytics.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 7874
URL = f"http://127.0.0.1:{PORT}/"
OUT = ROOT / "design" / "screenshots"
SIZES = {"1920": (1920, 1080), "1280": (1280, 800)}


def _both_sizes(page, base: str, settle_ms: int = 500) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for tag, (w, h) in SIZES.items():
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(settle_ms)
        page.screenshot(path=str(OUT / f"{base}_{tag}.png"), full_page=True)
        print("saved", f"{base}_{tag}.png")


def main() -> int:
    import analytics
    import upload_scheduler as sched
    import youtube_upload as yt

    scratch_cache = ROOT / "cache" / "analytics_cache.screenshot_scratch.json"
    scratch_log = ROOT / "cache" / "upload_log.screenshot_scratch.json"
    analytics.CACHE_FILE = scratch_cache
    sched.LOG_FILE = scratch_log

    uploads = {}
    sources = ["Podcast ep. 12", "Podcast ep. 12", "Interview - Founder", "Q&A stream", "Rant compilation"]
    hours = [9, 20, 14, 20, 9]
    for i in range(5):
        uploads[f"output/demo_job/clip_{i:02d}"] = {
            "video_id": f"DEMO{i}", "title": f"Demo clip {i}",
            "publish_at": f"2026-07-{i + 1:02d}T{hours[i]:02d}:00:00+05:30",
        }
    scratch_log.write_text(json.dumps({"uploads": uploads}), encoding="utf-8")

    video_rows = [
        ["DEMO0", 5200, 900.0, 55.0, 210, 12],
        ["DEMO1", 6100, 1100.0, 58.0, 260, 15],
        ["DEMO2", 1200, 260.0, 32.0, 40, 2],
        ["DEMO3", 8900, 1500.0, 62.0, 340, 20],
        ["DEMO4", 900, 150.0, 22.0, 15, 1],
    ]
    durations = {"DEMO0": 28.0, "DEMO1": 32.0, "DEMO2": 80.0, "DEMO3": 25.0, "DEMO4": 85.0}
    extras = {f"output/demo_job/clip_{i:02d}": {"duration_s": durations[f"DEMO{i}"],
                                               "source_name": sources[i]}
             for i in range(5)}

    def fake_clip_extra(key: str) -> dict:
        return extras.get(key, {})

    analytics._clip_extra = fake_clip_extra

    class FakeAnalytics:
        def reports(self_):
            return self_

        def query(self_, **kwargs):
            self_.kwargs = kwargs
            return self_

        def execute(self_):
            if "dimensions" in self_.kwargs:
                return {"rows": video_rows}
            return {"rows": [[22300, 3910.0, 47.0, 865, 50]]}

    yt.credentials_available = lambda: True
    yt.has_cached_token = lambda: True
    yt.build_analytics_service = lambda service=None: FakeAnalytics()

    import uvicorn
    from server import create_app
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_level="warning")
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
            page.goto(URL + "#/analytics", wait_until="load")
            page.wait_for_selector(".an-chart", timeout=15000)
            page.wait_for_timeout(800)
            _both_sizes(page, "an_01_overview")
            ok.append("overview")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        scratch_cache.unlink(missing_ok=True)
        scratch_log.unlink(missing_ok=True)
    print("captured:", ok)
    return 0 if len(ok) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
