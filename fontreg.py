"""Font registry: bundled fonts (assets/fonts) + user uploads (assets/user_fonts).

Reads each font's REAL family name from its `name` table (via fonttools) so ASS
styles reference the correct family — libass silently falls back to a default
font when the family name is wrong. Also resolves the fontsdir passed to the
subtitles filter: bundled-only stays assets/fonts (byte-identical to today);
when user fonts exist, a combined cache dir mirrors both so libass finds either.

Kept dependency-light and free of any captions import (captions imports THIS),
so there is no import cycle with style_preview.py."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from config import ROOT
from logutil import get_logger

log = get_logger("fontreg")

USER_FONTS_DIR = ROOT / "assets" / "user_fonts"
_COMBINED_DIR = ROOT / "cache" / "fonts_all"
_FONT_EXTS = (".ttf", ".otf")


def family_name(path: str | Path) -> str | None:
    """Real family name (name ID 1) of a font file, or None if unreadable.
    Raises ValueError for a file that does not parse as a font."""
    from fontTools.ttLib import TTFont
    try:
        tt = TTFont(str(path), fontNumber=0, lazy=True)
    except Exception as e:  # noqa: BLE001 — any parse failure means reject the file
        raise ValueError(f"not a valid font file: {e}") from e
    try:
        name = tt["name"].getDebugName(1) if "name" in tt else None
    finally:
        tt.close()
    return name.strip() if name else None


def _font_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.suffix.lower() in _FONT_EXTS)


def list_fonts(cfg: dict) -> list[dict]:
    """One entry per bundled + user font FILE: {family, path (repo-relative),
    source ('bundled'|'user')}. Files that fail to parse are skipped (logged)."""
    bundled = ROOT / cfg["captions"]["font_dir"]
    out: list[dict] = []
    for src, files in (("bundled", _font_files(bundled)),
                       ("user", _font_files(USER_FONTS_DIR))):
        for f in files:
            try:
                fam = family_name(f)
            except ValueError as e:
                log.warning("skipping unreadable font %s: %s", f.name, e)
                continue
            if not fam:
                log.warning("skipping font with no family name: %s", f.name)
                continue
            out.append({"family": fam,
                        "path": str(f.relative_to(ROOT)).replace("\\", "/"),
                        "source": src})
    return out


def font_path_for_family(cfg: dict, family: str) -> Path | None:
    """Absolute path of the first font file whose family matches (for cache
    keying / mtime); None if not found."""
    for f in list_fonts(cfg):
        if f["family"] == family:
            return ROOT / f["path"]
    return None


def fonts_dir(cfg: dict) -> Path:
    """Directory to hand the subtitles filter as fontsdir. Bundled-only → the
    bundled dir unchanged (preserves today's exact output). Once user fonts
    exist, mirror bundled + user files into cache/fonts_all and return that so
    libass can resolve either set from one directory."""
    bundled = ROOT / cfg["captions"]["font_dir"]
    users = _font_files(USER_FONTS_DIR)
    if not users:
        return bundled
    _COMBINED_DIR.mkdir(parents=True, exist_ok=True)
    for src in _font_files(bundled) + users:
        dst = _COMBINED_DIR / src.name
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            shutil.copyfile(src, dst)
    return _COMBINED_DIR


def register_upload(upload_path: str | Path) -> dict:
    """Validate an uploaded .ttf/.otf and copy it into assets/user_fonts/,
    returning {family, path, source}. Raises ValueError on a bad type or a file
    that does not parse as a font (so the UI can reject it)."""
    src = Path(upload_path)
    if src.suffix.lower() not in _FONT_EXTS:
        raise ValueError("font must be a .ttf or .otf file")
    fam = family_name(src)                     # raises if it does not parse
    if not fam:
        raise ValueError("could not read a family name from this font")
    USER_FONTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name) or "font.ttf"
    dst = USER_FONTS_DIR / safe
    shutil.copyfile(src, dst)
    log.info("registered user font %s (family=%s)", safe, fam)
    return {"family": fam, "path": str(dst.relative_to(ROOT)).replace("\\", "/"),
            "source": "user"}


if __name__ == "__main__":  # smoke: bundled fonts parse and resolve
    from config import load_config
    c = load_config()
    fonts = list_fonts(c)
    assert fonts, "no bundled fonts found"
    assert all(f["family"] for f in fonts), "a font is missing its family name"
    assert fonts_dir(c).exists()
    print("ok", [f["family"] for f in fonts])
