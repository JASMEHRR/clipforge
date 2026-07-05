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
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

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


def credentials_available() -> bool:
    p = os.environ.get("YOUTUBE_CLIENT_SECRETS", "")
    return bool(p) and Path(p).exists()


def has_cached_token() -> bool:
    return TOKEN_PATH.exists()


def authorize() -> None:
    """Run the OAuth browser flow. ONLY called from an explicit user action
    (UI button / CLI flag). Never runs during the automated build."""
    if not credentials_available():
        raise UploadError("no client secrets configured", detail=SETUP_INSTRUCTIONS)
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        os.environ["YOUTUBE_CLIENT_SECRETS"], SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    log.info("YouTube authorization complete; token cached")


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN_PATH.exists():
        raise UploadError("not authorized yet", detail=SETUP_INSTRUCTIONS)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(service=None):
    """Build the YouTube API client. Tests inject a mocked `service`."""
    if service is not None:
        return service
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_load_credentials(),
                 cache_discovery=False)


def build_request_body(metadata: dict, privacy: str = "private") -> dict:
    """Shorts-ready body from ClipMetadata. Hashtags go into the description
    (YouTube derives Shorts hashtags from there); title stays <=100 chars."""
    tags = [t.lstrip("#") for t in metadata.get("hashtags", [])][:15]
    description = metadata["description"].strip() + "\n\n" + " ".join(
        metadata.get("hashtags", []))
    return {
        "snippet": {
            "title": metadata["title"][:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }


def upload_clip(video_path: str | Path, metadata: dict,
                privacy: str = "private", service=None) -> dict:
    """Upload one clip. Returns {'video_id', 'url'}. Raises UploadQuotaError
    with a clear message on quota exhaustion — never crashes the app."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise UploadError(f"clip not found: {video_path}")
    yt = build_service(service)
    body = build_request_body(metadata, privacy)
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
