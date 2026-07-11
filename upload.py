"""ClipForge YouTube upload CLI.

Usage:
  python upload.py            upload the next eligible batch once, then exit
  python upload.py dry        show what WOULD upload, without uploading
  python upload.py watch      poll loop fallback (primary path is automatic,
                              see upload.auto_enabled in config.yaml)
  python upload.py report     28-day analytics + recommendations

First run opens a browser once to approve access (needs YOUTUBE_CLIENT_SECRETS
set — see youtube_upload.SETUP_INSTRUCTIONS / README), then never asks again.
"""
from __future__ import annotations

import sys

import config
import upload_scheduler
import youtube_upload
from errors import UploadError


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0].lower() if argv else "upload"
    cfg = config.load_config()
    upload_cfg = cfg.get("upload", {})

    if mode == "dry":
        log_data = upload_scheduler.load_log()
        candidates = upload_scheduler.find_candidates(cfg, log_data)
        batch = candidates[:upload_cfg.get("max_per_run", 2)]
        print(f"\n{len(candidates)} clip(s) eligible.\n")
        for c in batch:
            snippet = upload_scheduler.build_snippet(c["meta"])
            print(f"WOULD UPLOAD  [{c['score']:>3}]  {c['key']}")
            print(f"   title: {snippet['title']}")
            print(f"   tags:  {' '.join(snippet['hashtags'])}\n")
        return 0

    if not youtube_upload.credentials_available():
        print("YouTube is not configured.\n")
        print(youtube_upload.SETUP_INSTRUCTIONS)
        return 1
    if not youtube_upload.has_cached_token():
        print("Not authorized yet — authorizing now (a browser window will open)...")
        youtube_upload.authorize()

    if mode == "watch":
        upload_scheduler.watch(cfg)
        return 0

    youtube = youtube_upload.build_service()
    analytics = youtube_upload.build_analytics_service()

    if mode == "report":
        log_data = upload_scheduler.load_log()
        try:
            print(upload_scheduler.report(analytics, log_data))
        except UploadError as e:
            print(f"Could not fetch analytics: {e}")
            return 1
        return 0

    log_data = upload_scheduler.load_log()
    n = upload_scheduler.upload_batch(youtube, analytics, cfg, log_data,
                                      limit=upload_cfg.get("max_per_run", 2))
    print(f"Done. Uploaded {n} clip(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
