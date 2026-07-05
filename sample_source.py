"""Resolve the --sample source: primary URL → mirrors → synthetic fallback.
Downloads are cached in samples/ and sha256-verified (primary only; mirrors
may be a different public-domain film, so their hash is not enforced)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import requests

from config import ROOT, file_hash
from logutil import get_logger

log = get_logger("sample")

SAMPLE_PATH = ROOT / "samples" / "sample.mp4"


def resolve_sample(cfg: dict) -> Path:
    scfg = cfg["sample"]
    expected = scfg.get("sha256", "")

    if SAMPLE_PATH.exists():
        if not expected or file_hash(SAMPLE_PATH) == expected:
            log.info("sample cached: %s", SAMPLE_PATH)
            return SAMPLE_PATH
        log.warning("cached sample hash mismatch — re-downloading")

    urls = [scfg["primary_url"]] + list(scfg.get("mirrors", []))
    for i, url in enumerate(urls):
        try:
            _download(url, SAMPLE_PATH)
            actual = file_hash(SAMPLE_PATH)
            if i == 0 and expected and actual != expected:
                log.warning("primary sample sha256 mismatch (%s) — trying mirror",
                            actual[:12])
                continue
            log.info("sample downloaded from %s (sha256 %s)", url, actual[:12])
            return SAMPLE_PATH
        except Exception as e:  # noqa: BLE001 — every failure falls through to next mirror
            log.warning("sample source failed (%s): %s", url, e)

    if not cfg["sample"].get("synthetic_fallback", True):
        raise RuntimeError("all sample sources failed and synthetic_fallback is off")
    log.warning("all sample mirrors failed — generating synthetic sample "
                "(gate items pass mechanically; re-verify with a real sample)")
    from scripts.make_synthetic_sample import make_sample
    return make_sample(SAMPLE_PATH)


def _download(url: str, dest: Path, timeout: int = 600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    h = hashlib.sha256()
    with requests.get(url, stream=True, timeout=(20, timeout)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                h.update(chunk)
    if tmp.stat().st_size < 100_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("downloaded file suspiciously small")
    tmp.replace(dest)
