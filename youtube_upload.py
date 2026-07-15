"""YouTube upload (Data API v3, OAuth via google-auth-oauthlib).

BUILD CONSTRAINT: the browser OAuth flow is NEVER started automatically —
not at import, not during the build, not by the UI on its own. authorize()
only runs when the user explicitly clicks "Authorize" AND a client-secrets
file exists. All upload logic is verified by unit tests with mocked API
responses. Default privacy: private."""
from __future__ import annotations

import os
from pathlib import Path

from config import ROOT
from errors import UploadError, UploadQuotaError
from logutil import get_logger

log = get_logger("upload")

TOKEN_PATH = ROOT / "cache" / "youtube_token.json"


def token_path(account: str = "default") -> Path:
    """Per-account OAuth token cache. The 'default' account keeps the legacy
    path so existing single-account installs stay authorized untouched."""
    if not account or account == "default":
        return TOKEN_PATH
    safe = "".join(c for c in account if c.isalnum() or c in "-_").lower()
    if not safe:
        raise UploadError(f"invalid account name: {account!r}")
    return ROOT / "cache" / f"youtube_token_{safe}.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

SETUP_INSTRUCTIONS = """### YouTube upload — one-time setup (manual)
1. Go to https://console.cloud.google.com/ → create/select a project.
2. Enable the **YouTube Data API v3** (APIs & Services → Library).
3. APIs & Services → Credentials → **Create credentials → OAuth client ID**
   → application type **Desktop app** → download the JSON file.
4. Save it somewhere private and set in `.env`:
   `YOUTUBE_CLIENT_SECRETS=C:\\path\\to\\client_secret.json`
5. Restart ClipForge and click **Authorize YouTube** — a browser window will
   open ONCE to grant access; the token is cached locally after that.
Uploads default to **private** — flip them public in YouTube Studio."""


# Hard dev/test guard. When CLIPFORGE_DRY_RUN is set, nothing in this module
# reaches the real YouTube API — build_service hands back a sentinel, uploads
# return a fake video, deletes/status no-op. This is a floor, not a flag the
# UI can override: sync/upload endpoints can never touch a real channel while
# it's set, regardless of whatever OAuth token exists on the machine.
_DRY_SERVICE = object()   # handed to callers in dry-run; never used for a call


def dry_run() -> bool:
    return os.environ.get("CLIPFORGE_DRY_RUN", "").strip().lower() \
        not in ("", "0", "false", "no", "off")


def credentials_available() -> bool:
    p = os.environ.get("YOUTUBE_CLIENT_SECRETS", "")
    return bool(p) and Path(p).exists()


def has_cached_token(account: str = "default") -> bool:
    return token_path(account).exists()


def authorized(account: str = "default") -> bool:
    """True if the app can talk to the channel right now (credentials
    configured AND a token has been granted). Any probe failure counts as
    not connected — shared by every route that gates on YouTube access."""
    try:
        return bool(credentials_available() and has_cached_token(account))
    except Exception:  # noqa: BLE001
        return False


def authorize(account: str = "default") -> None:
    """Run the OAuth browser flow for one destination account. ONLY called
    from an explicit user action (UI button / CLI flag). Never runs during
    the automated build."""
    if not credentials_available():
        raise UploadError("no client secrets configured", detail=SETUP_INSTRUCTIONS)
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        os.environ["YOUTUBE_CLIENT_SECRETS"], SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    path = token_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    log.info("YouTube authorization complete for account '%s'; token cached",
             account)


def _load_credentials(account: str = "default"):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    path = token_path(account)
    if not path.exists():
        raise UploadError(f"account '{account}' not authorized yet",
                          detail=SETUP_INSTRUCTIONS)
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(service=None, account: str = "default"):
    """Build the YouTube API client. Tests inject a mocked `service`."""
    if service is not None:
        return service
    if dry_run():
        log.warning("[DRY RUN] not building a real YouTube client")
        return _DRY_SERVICE
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_load_credentials(account),
                 cache_discovery=False)


def build_analytics_service(service=None, account: str = "default"):
    """Build the YouTube Analytics API client. Tests inject a mocked `service`."""
    if service is not None:
        return service
    if dry_run():
        return _DRY_SERVICE
    from googleapiclient.discovery import build
    return build("youtubeAnalytics", "v2",
                 credentials=_load_credentials(account),
                 cache_discovery=False)


def build_request_body(metadata: dict, privacy: str = "private",
                       publish_at: str | None = None,
                       category_id: str | None = None) -> dict:
    """Shorts-ready body from ClipMetadata. Hashtags go into the description
    (YouTube derives Shorts hashtags from there); title stays <=100 chars.
    `publish_at` (ISO 8601 UTC, e.g. '2026-07-11T06:30:00Z') schedules the
    video; YouTube requires privacyStatus='private' for scheduled videos."""
    tags = [t.lstrip("#") for t in metadata.get("hashtags", [])][:15]
    description = metadata["description"].strip() + "\n\n" + " ".join(
        metadata.get("hashtags", []))
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        status["publishAt"] = publish_at
    if metadata.get("synthetic"):
        # Avatar Host clips (cloned voice + generated frames) MUST carry
        # YouTube's altered/synthetic-content disclosure (Data API field
        # added Oct 2024). Absent flag -> key absent -> body unchanged.
        status["containsSyntheticMedia"] = True
    return {
        "snippet": {
            "title": metadata["title"][:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": category_id or "22",
        },
        "status": status,
    }


def upload_clip(video_path: str | Path, metadata: dict,
                privacy: str = "private", service=None,
                publish_at: str | None = None,
                category_id: str | None = None) -> dict:
    """Upload one clip. Returns {'video_id', 'url'}. Raises UploadQuotaError
    with a clear message on quota exhaustion — never crashes the app."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise UploadError(f"clip not found: {video_path}")
    if dry_run():
        import hashlib
        vid = "DRYRUN" + hashlib.sha1(str(video_path).encode()).hexdigest()[:9]
        log.warning("[DRY RUN] not uploading %s (privacy=%s); pretend video %s",
                    video_path.name, privacy, vid)
        return {"video_id": vid, "url": f"https://youtu.be/{vid}"}
    yt = build_service(service)
    body = build_request_body(metadata, privacy, publish_at, category_id)
    media = _media_upload(video_path)
    try:
        request = yt.videos().insert(part="snippet,status", body=body,
                                     media_body=media)
        response = _execute_resumable(request)
    except UploadError:
        raise
    except Exception as e:  # noqa: BLE001 — classify API errors
        raise _classify(e) from e
    vid = response.get("id", "")
    log.info("uploaded %s -> https://youtu.be/%s (privacy=%s)",
             video_path.name, vid, privacy)
    return {"video_id": vid, "url": f"https://youtu.be/{vid}"}


def delete_video(video_id: str, service=None) -> None:
    """Delete an uploaded (scheduled-but-not-yet-public) video. Used to
    un-schedule a pre-booked clip. ~50 quota units."""
    if dry_run():
        log.warning("[DRY RUN] not deleting video %s", video_id)
        return
    yt = build_service(service)
    try:
        yt.videos().delete(id=video_id).execute()
    except Exception as e:  # noqa: BLE001
        raise _classify(e) from e
    log.info("deleted video %s", video_id)


def video_status(video_ids: list[str], service=None) -> dict[str, str]:
    """Live privacyStatus per video id ('public' | 'private' | 'unlisted'),
    for splitting scheduled vs published. Missing ids (deleted on YouTube) are
    absent from the result. 1 quota unit per call; ids batched 50 at a time."""
    if not video_ids:
        return {}
    if dry_run():
        return {}   # no live status in dry-run -> classify falls back to clock
    yt = build_service(service)
    out: dict[str, str] = {}
    ids = [v for v in video_ids if v]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            resp = yt.videos().list(part="status", id=",".join(chunk)).execute()
        except Exception as e:  # noqa: BLE001 — status is best-effort context
            log.warning("video status lookup failed: %s", e)
            continue
        for item in resp.get("items", []):
            out[item["id"]] = item.get("status", {}).get("privacyStatus", "")
    return out


def _media_upload(video_path: Path):
    from googleapiclient.http import MediaFileUpload
    return MediaFileUpload(str(video_path), mimetype="video/mp4",
                           resumable=True, chunksize=4 * 1024 * 1024)


def _execute_resumable(request) -> dict:
    response = None
    while response is None:
        try:
            _, response = request.next_chunk()
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
    return response


def _classify(e: Exception) -> UploadError:
    text = str(e)
    if "quotaExceeded" in text or "uploadLimitExceeded" in text:
        return UploadQuotaError(
            "YouTube API quota exhausted for today. Quotas reset at midnight "
            "Pacific time; try again then, or request a higher quota in the "
            "Google Cloud console.", detail=text[:300])
    if "401" in text or "invalid_grant" in text:
        return UploadError("YouTube authorization expired — click "
                           "'Authorize YouTube' again.", detail=text[:300])
    return UploadError("YouTube upload failed", detail=text[:500])
