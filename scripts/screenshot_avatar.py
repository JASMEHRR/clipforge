"""DEV-ONLY Avatar Host verify harness: boot the REAL server factory keyless,
drive the #/avatar workspace end to end in headless Chromium, and save numbered
screenshots to ui_verify/ proving each acceptance item. Console + failed-request
logs land in ui_verify/console.txt.

Static-PNG render mode is forced (avatar.animation.enabled: false) so the render
proof is fast and never invokes MuseTalk. config.local.yaml is backed up and
restored around the run — the user's real overrides are left untouched.

Not part of the app or requirements.txt (playwright is a dev install). Run:
    .venv\\Scripts\\python scripts\\screenshot_avatar.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 7873
URL = f"http://127.0.0.1:{PORT}/"
OUT = ROOT / "ui_verify"
LOCAL = ROOT / "config.local.yaml"

JOB = "20260713-185729_url"   # real job: 10 clips + .done_transcribe.json


def _force_static_mode() -> str | None:
    """Merge avatar.animation.enabled=false into config.local.yaml so the render
    uses the fast static-PNG path. Returns the original file text (or None if it
    didn't exist) so the caller can restore it."""
    import yaml
    original = LOCAL.read_text(encoding="utf-8") if LOCAL.exists() else None
    existing = yaml.safe_load(original) if original else {}
    existing = existing or {}
    existing.setdefault("avatar", {}).setdefault("animation", {})["enabled"] = False
    LOCAL.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
    return original


def _restore_local(original: str | None) -> None:
    if original is None:
        LOCAL.unlink(missing_ok=True)
    else:
        LOCAL.write_text(original, encoding="utf-8")


def _wait_ready(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(URL + "api/presets", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("server never became ready")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    console_log: list[str] = []
    ok: list[str] = []

    original_local = _force_static_mode()
    env = dict(os.environ, GEMINI_API_KEY="", OPENROUTER_API_KEY="",
               CLIPFORGE_PORT=str(PORT))
    server = subprocess.Popen(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c",
         "import uvicorn; from server import create_app; "
         f"uvicorn.run(create_app(), host='127.0.0.1', port={PORT}, "
         "log_level='warning')"],
        cwd=str(ROOT), env=env)

    def shot(name: str, selector: str | None = None) -> None:
        # viewport shots (not full_page) — the clips list is hundreds of real
        # rows, so a full_page capture is unreadably tall. Scroll the relevant
        # element into view first so each item is actually legible.
        if selector:
            try:
                page.locator(selector).first.scroll_into_view_if_needed(timeout=4000)
                page.wait_for_timeout(250)
            except Exception:  # noqa: BLE001 — fall back to whatever's shown
                pass
        else:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(150)
        page.screenshot(path=str(OUT / name))
        print("saved", name)

    try:
        _wait_ready()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", lambda m: console_log.append(
                f"[{m.type}] {m.text}") if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: console_log.append(f"[pageerror] {e}"))
            page.on("requestfailed", lambda r: console_log.append(
                f"[requestfailed] {r.url} — {r.failure}"))
            # 4xx/5xx completed responses (a 404 is not a "requestfailed")
            page.on("response", lambda r: console_log.append(
                f"[http{r.status}] {r.url}") if r.status >= 400 else None)
            # prove the voice-preview call actually succeeds (item 3)
            preview_ok: list[str] = []
            page.on("response", lambda r: preview_ok.append(r.url)
                    if "/api/avatar/voice-preview" in r.url and r.request.method == "POST"
                    and r.status == 200 else None)

            # ---- item 1: workspace renders on top on a fresh load ------------
            page.goto(URL + "#/avatar", wait_until="load")
            page.wait_for_selector("text=Pick an avatar, choose a clip",
                                   timeout=15000)
            page.wait_for_selector("button:has-text('Choose clip')", timeout=15000)
            page.wait_for_timeout(1200)   # avatar images fetch
            shot("01_initial.png")
            ok.append("panel-renders")
            shot("02_avatar_field.png", ".avatar-picker")

            # choose an avatar: prefer an inline pickable thumb, else the modal
            picked = False
            inline = page.locator("[data-avatar-pick]")
            if inline.count():
                inline.first.click()
                picked = True
            else:
                btn = page.get_by_role("button", name=re.compile("browse all", re.I))
                if btn.count():
                    btn.first.click()
                    try:
                        page.wait_for_selector("dialog.dialog .dialog-grid .pick",
                                               timeout=8000)
                        page.locator("dialog.dialog .dialog-grid .pick").first.click()
                        page.wait_for_selector("dialog.dialog", state="detached",
                                               timeout=8000)
                        picked = True
                    except Exception as e:  # noqa: BLE001
                        console_log.append(f"[harness] avatar pick failed: {e}")
            if picked:
                page.wait_for_timeout(600)
                ok.append("avatar-picked")

            # ---- item 2: "Choose clip" dialog with real clips ---------------
            page.get_by_role("button", name="Choose clip").click()
            page.wait_for_selector("dialog.dialog .dialog-grid .pick", timeout=15000)
            page.wait_for_timeout(600)
            shot("03_clip_dialog.png", "dialog.dialog")
            ok.append("clip-dialog")
            page.locator("dialog.dialog .dialog-grid .pick").first.click()
            page.wait_for_selector("dialog.dialog", state="detached", timeout=8000)
            page.wait_for_selector(".avatar-clip-card", timeout=8000)
            shot("04_clip_card.png", ".avatar-clip-card")
            ok.append("clip-card")

            probe = bool(os.environ.get("AVATAR_PROBE"))   # fast: skip render

            # ---- item 4: scripts editable + preview frame on real footage ---
            page.wait_for_selector("textarea", timeout=30000)
            page.wait_for_selector(".avatar-preview-frame", timeout=10000)
            page.wait_for_timeout(800)
            shot("05_scripts.png", "textarea")
            ok.append("scripts")

            # ---- item 3: voice modal + per-voice preview (generating state) --
            try:
                page.locator(".field:has(label:text-is('Voice')) button").first.click()
                page.wait_for_selector("dialog.dialog .voice-preview-btn", timeout=8000)
                hear = page.locator("dialog.dialog .voice-preview-btn").first
                hear.click()
                page.wait_for_timeout(120)         # catch the "generating…" state
                shot("06_voice_generating.png", "dialog.dialog")
                # wait for the synth POST to come back 200
                for _ in range(60):
                    if preview_ok:
                        break
                    page.wait_for_timeout(200)
                if preview_ok:
                    ok.append("voice-preview")
                shot("06b_voice_ready.png", "dialog.dialog")
                page.locator("dialog.dialog").first.press("Escape")
                page.wait_for_selector("dialog.dialog", state="detached", timeout=8000)
            except Exception as e:  # noqa: BLE001 — still continue to render
                console_log.append(f"[harness] voice preview step failed: {e}")

            # ---- items 5/6/7: render + progress + result --------------------
            if not probe:
                _render_and_result(page, shot, ok)

            browser.close()
    except Exception as e:  # noqa: BLE001 — record, still restore + report
        console_log.append(f"[harness] FATAL: {type(e).__name__}: {e}")
        print("harness error:", e)
    finally:
        server.terminate()
        _restore_local(original_local)

    errors = [ln for ln in console_log
              if ln.startswith("[error]") or ln.startswith("[pageerror]")
              or ln.startswith("[http4") or ln.startswith("[http5")
              or "FATAL" in ln]
    benign = [ln for ln in console_log if "ERR_ABORTED" in ln]
    (OUT / "console.txt").write_text(
        "=== console/network log (errors, warnings, 4xx/5xx, aborts) ===\n"
        + ("\n".join(console_log) if console_log else "(none)")
        + f"\n\n=== REAL ERROR COUNT (excl. benign media ERR_ABORTED): {len(errors)} ===\n"
        + "\n".join(errors)
        + f"\n\n=== benign media ERR_ABORTED (video preload cancels): {len(benign)} ===\n"
        + f"=== STEPS OK: {ok} ===\n", encoding="utf-8")
    print("captured:", ok)
    print("real errors:", len(errors), "| benign aborts:", len(benign))
    return 0 if not errors and len(ok) >= 7 else 1


def _render_and_result(page, shot, ok) -> None:
    page.get_by_role("button", name="Render").click()
    page.wait_for_selector(".avatar-progress", state="visible", timeout=10000)
    # wait until the ETA line shows an elapsed clock + remaining estimate
    try:
        page.wait_for_function(
            "() => { const s = document.querySelector('.avatar-eta');"
            " return s && /left|working|estimating|Done/.test(s.textContent || ''); }",
            timeout=20000)
    except Exception:  # noqa: BLE001 — still capture whatever is shown
        pass
    page.wait_for_timeout(1200)
    shot("07_progress.png", ".avatar-progress")
    ok.append("progress")

    # ---- item 6: refresh mid-render → reattach to the in-flight run -------
    try:
        page.reload(wait_until="load")
        page.wait_for_timeout(1500)
        # either progress resumed (still running) or the result is already up
        page.wait_for_selector(".avatar-progress, button:has-text('Approve')",
                               timeout=15000)
        shot("08_reattach.png", ".avatar-progress")
        ok.append("reattach")
    except Exception as e:  # noqa: BLE001
        print("reattach step:", e)

    # ---- item 7: result video + approve row ------------------------------
    page.get_by_role("button", name="Approve").wait_for(timeout=300000)
    page.wait_for_timeout(1500)
    shot("09_result.png", "button:has-text('Approve')")
    ok.append("result")


if __name__ == "__main__":
    raise SystemExit(main())
