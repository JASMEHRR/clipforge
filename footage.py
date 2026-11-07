"""Free stock footage for Create mode: Pexels → Pixabay → NASA, cached.

Providers are tried in config order (`create.footage.providers`); a provider
without its API key in .env is skipped (PEXELS_API_KEY / PIXABAY_API_KEY -
both free sign-ups, no card). NASA's Image and Video Library needs no key and
is used for images only, accepted only when a query word appears in the
item's title/keywords (so "octopus" never gets a galaxy).

Every returned asset carries its provider, page URL, author and license so
create.py can credit sources in the description. Searches are cached for
`search_cache_hours` (Pixabay's API terms require 24h caching) and downloaded
files are cached by provider+id, so re-runs and repeat topics cost nothing.

Public API:
  find_asset(query, need_s, cfg, used) -> dict | None
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

from config import ROOT, load_config
from errors import ClipForgeError
from ffutil import FFmpegError, probe
from logutil import get_logger

log = get_logger("footage")

_USER_AGENT = "ClipForge/2.0 (+https://github.com/; create-mode footage)"
PEXELS_URL = "https://api.pexels.com/videos/search"
PIXABAY_URL = "https://pixabay.com/api/videos/"
NASA_SEARCH_URL = "https://images-api.nasa.gov/search"
NASA_ASSET_URL = "https://images-api.nasa.gov/asset/{nasa_id}"
LICENSES = {
    "pexels": "Pexels License (free to use, no attribution required)",
    "pixabay": "Pixabay Content License (free to use, no attribution required)",
    "nasa": "NASA media (generally not copyrighted; no NASA endorsement implied)",
}
_KEY_ENV = {"pexels": "PEXELS_API_KEY", "pixabay": "PIXABAY_API_KEY"}
_STOPWORDS = {"with", "from", "that", "this", "into", "over", "under", "their",
              "close", "view", "shot", "video", "image", "photo"}


class FootageError(ClipForgeError):
    stage = "footage"


def _fcfg(cfg: dict) -> dict:
    return (cfg.get("create") or {}).get("footage") or {}


def _cache_root(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["cache_dir"] / "footage"


def query_words(query: str) -> set[str]:
    """Lower-cased content words (>=4 chars, not filler) used for NASA relevance."""
    return {w for w in re.findall(r"[a-z]+", query.lower())
            if len(w) >= 4 and w not in _STOPWORDS}


# ------------------------------------------------------------------ HTTP layer

def _get_json(url: str, params: dict, headers: dict, timeout_s: float) -> dict:
    """GET → JSON with one retry on 429/5xx/network error. Raises FootageError."""
    last: str = ""
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, timeout=timeout_s,
                             headers={"User-Agent": _USER_AGENT, **headers})
        except requests.RequestException as e:
            last = str(e)[:200]
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as e:
                    raise FootageError(f"non-JSON response from {url}") from e
            if r.status_code in (401, 403):
                raise FootageError(f"{url} rejected the API key "
                                   f"(HTTP {r.status_code})", retryable=False)
            last = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        if attempt == 0:
            time.sleep(2.0)
    raise FootageError(f"request failed: {url}", detail=last)


def _cached_search(provider: str, query: str, cfg: dict, fetch) -> dict:
    """Return the cached search JSON when fresh, else call fetch() and cache it."""
    d = _cache_root(cfg) / "search"
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{provider}|{query.lower()}".encode()).hexdigest()[:16]
    path = d / f"{provider}_{key}.json"
    ttl = float(_fcfg(cfg).get("search_cache_hours", 24)) * 3600
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("corrupt search cache %s - refetching", path.name)
    data = fetch()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return data


def _download(url: str, dest: Path, cfg: dict) -> Path:
    """Stream to dest (size-capped), verify it decodes with ffprobe."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    fc = _fcfg(cfg)
    cap = int(float(fc.get("max_download_mb", 60)) * 1024 * 1024)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        with requests.get(url, stream=True, timeout=float(fc.get("timeout_s", 30)),
                          headers={"User-Agent": _USER_AGENT}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    written += len(chunk)
                    if written > cap:
                        raise FootageError(f"file exceeds max_download_mb: {url}",
                                           retryable=False)
                    f.write(chunk)
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise FootageError(f"download failed: {url}", detail=str(e)[:200]) from e
    except FootageError:
        tmp.unlink(missing_ok=True)
        raise
    try:
        probe(tmp)
    except FFmpegError as e:
        tmp.unlink(missing_ok=True)
        raise FootageError(f"downloaded file does not decode: {url}",
                           detail=e.message) from e
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------ providers

def _short_side_ok(w: int, h: int, min_short: int) -> bool:
    return min(int(w or 0), int(h or 0)) >= min_short


def pick_pexels(data: dict, need_s: float, min_short: int, used: set,
                portrait: bool = False) -> dict | None:
    """Choose the best Pexels video + file variant. Pure (testable). Ranking:
    wanted orientation, then long enough for the shot, then short side
    closest to 1080."""
    best, best_key = None, None
    for v in data.get("videos") or []:
        vid = f"pexels:{v.get('id')}"
        if vid in used:
            continue
        files = [f for f in v.get("video_files") or []
                 if f.get("file_type") == "video/mp4" and f.get("link")
                 and _short_side_ok(f.get("width"), f.get("height"), min_short)]
        if not files:
            continue
        # variant whose short side is closest to 1080 (enough detail, small file)
        f = min(files, key=lambda x: abs(min(x["width"], x["height"]) - 1080))
        oriented = (int(f["height"]) > int(f["width"])) == portrait
        long_enough = float(v.get("duration") or 0) >= need_s
        key = (oriented, long_enough, -abs(min(f["width"], f["height"]) - 1080))
        if best_key is None or key > best_key:
            best_key = key
            best = {"kind": "video", "provider": "pexels", "id": vid,
                    "url": f["link"], "page_url": v.get("url", ""),
                    "author": (v.get("user") or {}).get("name", ""),
                    "width": int(f["width"]), "height": int(f["height"]),
                    "duration": float(v.get("duration") or 0)}
    return best


def pick_pixabay(data: dict, need_s: float, min_short: int, max_bytes: int,
                 used: set, portrait: bool = False) -> dict | None:
    """Choose the best Pixabay video + size variant. Pure (testable). Same
    ranking as pick_pexels."""
    best, best_key = None, None
    for h in data.get("hits") or []:
        vid = f"pixabay:{h.get('id')}"
        if vid in used:
            continue
        variants = [x for x in (h.get("videos") or {}).values()
                    if isinstance(x, dict) and x.get("url")
                    and _short_side_ok(x.get("width"), x.get("height"), min_short)
                    and (not x.get("size") or int(x["size"]) <= max_bytes)]
        if not variants:
            continue
        x = min(variants, key=lambda y: abs(min(y["width"], y["height"]) - 1080))
        oriented = (int(x["height"]) > int(x["width"])) == portrait
        long_enough = float(h.get("duration") or 0) >= need_s
        key = (oriented, long_enough, -abs(min(x["width"], x["height"]) - 1080))
        if best_key is None or key > best_key:
            best_key = key
            best = {"kind": "video", "provider": "pixabay", "id": vid,
                    "url": x["url"], "page_url": h.get("pageURL", ""),
                    "author": h.get("user", ""),
                    "width": int(x["width"]), "height": int(x["height"]),
                    "duration": float(h.get("duration") or 0)}
    return best


def pick_nasa(data: dict, query: str, used: set) -> dict | None:
    """First NASA image whose title/keywords share a content word with the
    query. Pure (testable). Returns the item with its nasa_id; the full-size
    URL is resolved separately via the asset endpoint."""
    words = query_words(query)
    if not words:
        return None
    for item in ((data.get("collection") or {}).get("items") or []):
        meta = (item.get("data") or [{}])[0]
        nid = meta.get("nasa_id")
        if not nid or f"nasa:{nid}" in used or meta.get("media_type") != "image":
            continue
        hay = " ".join([str(meta.get("title", ""))]
                       + [str(k) for k in meta.get("keywords") or []]).lower()
        if words & set(re.findall(r"[a-z]+", hay)):
            return {"kind": "image", "provider": "nasa", "id": f"nasa:{nid}",
                    "nasa_id": nid,
                    "page_url": f"https://images.nasa.gov/details/{nid}",
                    "author": meta.get("center", "NASA") or "NASA"}
    return None


def _nasa_image_url(nasa_id: str, timeout_s: float) -> str:
    data = _get_json(NASA_ASSET_URL.format(nasa_id=nasa_id), {}, {}, timeout_s)
    hrefs = [i.get("href", "") for i in (data.get("collection") or {}).get("items") or []]
    for suffix in ("~large.jpg", "~medium.jpg", "~orig.jpg"):
        for h in hrefs:
            if h.lower().endswith(suffix):
                return h.replace("http://", "https://", 1)
    raise FootageError(f"no usable image file for NASA item {nasa_id}")


def _try_provider(name: str, query: str, need_s: float, cfg: dict,
                  used: set, portrait: bool) -> dict | None:
    fc = _fcfg(cfg)
    timeout_s = float(fc.get("timeout_s", 30))
    min_short = int(fc.get("min_height", 720))
    max_bytes = int(float(fc.get("max_download_mb", 60)) * 1024 * 1024)
    files = _cache_root(cfg) / "files"

    if name in _KEY_ENV:
        key = os.environ.get(_KEY_ENV[name], "").strip()
        if not key:
            log.info("footage: %s skipped (%s not set)", name, _KEY_ENV[name])
            return None
    if name == "pexels":
        orient = "portrait" if portrait else "landscape"
        data = _cached_search(f"{name}-{orient}", query, cfg, lambda: _get_json(
            PEXELS_URL, {"query": query, "orientation": orient,
                         "size": "medium", "per_page": 15},
            {"Authorization": key}, timeout_s))
        pick = pick_pexels(data, need_s, min_short, used, portrait)
        ext = ".mp4"
    elif name == "pixabay":
        data = _cached_search(name, query, cfg, lambda: _get_json(
            PIXABAY_URL, {"key": key, "q": query[:100], "safesearch": "true",
                          "per_page": 20}, {}, timeout_s))
        pick = pick_pixabay(data, need_s, min_short, max_bytes, used, portrait)
        ext = ".mp4"
    elif name == "nasa":
        data = _cached_search(name, query, cfg, lambda: _get_json(
            NASA_SEARCH_URL, {"q": query, "media_type": "image",
                              "page_size": 25}, {}, timeout_s))
        pick = pick_nasa(data, query, used)
        if pick:
            pick["url"] = _nasa_image_url(pick["nasa_id"], timeout_s)
        ext = ".jpg"
    else:
        raise FootageError(f"unknown footage provider '{name}'", retryable=False)
    if not pick:
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", pick["id"])
    pick["path"] = str(_download(pick["url"], files / f"{safe_id}{ext}", cfg))
    pick["license"] = LICENSES[name]
    return pick


def find_asset(query: str, need_s: float, cfg: dict | None = None,
               used: set | None = None, portrait: bool = False) -> dict | None:
    """Best available asset for one narrated line, or None (caller renders a
    colour card). Never raises for provider/network trouble - each failure is
    logged and the next provider is tried. `used` holds asset ids already in
    this video; the chosen id is added to it. `portrait` picks the preferred
    orientation (landscape by default: the long video is 16:9 and Shorts
    centre-crop the same footage)."""
    cfg = cfg or load_config()
    used = used if used is not None else set()
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not query:
        return None
    for name in _fcfg(cfg).get("providers", ["pexels", "pixabay", "nasa"]):
        try:
            asset = _try_provider(str(name), query, float(need_s), cfg, used,
                                  bool(portrait))
        except FootageError as e:
            log.warning("footage: %s failed for '%s': %s", name, query, e)
            continue
        if asset:
            used.add(asset["id"])
            log.info("footage: '%s' -> %s %s", query, asset["provider"], asset["id"])
            return asset
    log.info("footage: nothing found for '%s' (colour card fallback)", query)
    return None
