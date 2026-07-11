"""DEV-ONLY proof for the Activity tab + tab-switch survival (item 3).

Runs the real server in-process and fakes pipeline.run_job with a staged
progress sequence (~7s) so the run is deterministic without a heavy real
render. The pipeline runs on a daemon thread exactly as in production, so
this genuinely exercises: the Activity list (/api/runs), live progress, and
the key claim — a run keeps going while its tab is backgrounded and the
progress view re-syncs on return.

Run:  .venv\\Scripts\\python scripts\\screenshot_activity.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 7873
URL = f"http://127.0.0.1:{PORT}/"
OUT = ROOT / "design" / "screenshots"
SIZES = {"1920": (1920, 1080), "1280": (1280, 800)}


def _both_sizes(page, base: str, settle_ms: int = 400) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for tag, (w, h) in SIZES.items():
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(settle_ms)
        page.screenshot(path=str(OUT / f"{base}_{tag}.png"), full_page=True)
        print("saved", f"{base}_{tag}.png")


def main() -> int:
    import pipeline

    job_dir = ROOT / "output" / "20260711-999999_activitydemo"
    job_dir.mkdir(parents=True, exist_ok=True)

    def fake_run_job(source, cfg=None, tracker=None, job_dir=None, cancel=None,
                     **kw):
        # a realistic-looking staged run, ~7s total
        for key, msg, secs in [("ingest", "downloading", 1.5),
                               ("scenes", "finding scenes", 1.5),
                               ("transcribe", "listening", 1.5),
                               ("highlights", "ranking moments", 1.0),
                               ("render", "making clips", 1.5)]:
            tracker.start(key, msg)
            tracker.update(key, 0.5, msg)
            time.sleep(secs)
            tracker.finish(key)
        tracker.finish("done", "completed")
        return {"job_id": job_dir.name, "job_dir": str(job_dir), "clips": []}

    pipeline.run_job = fake_run_job
    pipeline.new_job_dir = lambda cfg, src: job_dir

    import uvicorn
    from server import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1",
                                           port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    ok = []
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.1)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            page.goto(URL, wait_until="load")

            # start a run straight through the API, then land on its live view
            run_id = page.evaluate(
                """async () => (await (await fetch('/api/runs', {method:'POST',
                   headers:{'Content-Type':'application/json'},
                   body: JSON.stringify({source:'https://example.com/v'})}))
                   .json()).run_id""")
            print("started run", run_id)

            # 1. Activity tab shows it running
            page.goto(URL + "#/activity", wait_until="load")
            page.wait_for_selector(".activity-row .badge-live", timeout=10000)
            _both_sizes(page, "activity_01_running")
            ok.append("running")

            # 2. TAB-SWITCH SURVIVAL: open the live progress view, then
            # background this tab for the whole run via a second foreground tab.
            page.goto(URL + f"#/run/{run_id}", wait_until="load")
            page.wait_for_selector(".progress-wrap", timeout=10000)
            other = ctx.new_page()
            other.goto(URL + "#/settings", wait_until="load")
            other.bring_to_front()          # backgrounds `page` (real visibilitychange)
            time.sleep(8)                    # run finishes entirely while hidden
            page.bring_to_front()            # -> visibilitychange -> immediate re-sync

            # on return the view catches up and routes to the finished run
            page.wait_for_url("**/results/**", timeout=6000)
            print("returned to visible tab -> navigated to", page.url)
            ok.append("survived")

            # 3. Activity tab now shows it done
            page.goto(URL + "#/activity", wait_until="load")
            page.wait_for_selector(".activity-row .badge-ok", timeout=10000)
            _both_sizes(page, "activity_02_done")
            ok.append("done")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    print("captured:", ok)
    return 0 if len(ok) == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
