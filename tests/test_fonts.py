"""Font registry + real-pipeline preview generation."""
import fontreg
import style_preview
from config import load_config


def test_list_fonts_reads_families():
    fonts = fontreg.list_fonts(load_config())
    assert fonts, "no bundled fonts discovered"
    assert all(f["family"] for f in fonts)                 # real family names
    assert any(f["family"] == "Montserrat ExtraBold" for f in fonts)
    assert all(f["source"] == "bundled" for f in fonts)    # no user fonts yet


def test_register_upload_rejects_non_font(tmp_path, monkeypatch):
    monkeypatch.setattr(fontreg, "USER_FONTS_DIR", tmp_path / "user_fonts")
    bad = tmp_path / "fake.ttf"
    bad.write_bytes(b"this is not a font")
    try:
        fontreg.register_upload(bad)
        assert False, "expected ValueError for a non-font .ttf"
    except ValueError:
        pass


def test_register_upload_rejects_bad_extension(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    try:
        fontreg.register_upload(txt)
        assert False, "expected ValueError for a non-font extension"
    except ValueError:
        pass


def test_preview_png_caches():
    cfg = load_config()
    p1 = style_preview.preview_png("karaoke-pop", cfg=cfg)
    p2 = style_preview.preview_png("karaoke-pop", cfg=cfg)
    assert p1 == p2 and p1.exists() and p1.stat().st_size > 0


def test_font_override_changes_render():
    # Two different families through the real burn MUST differ — proves the
    # override reaches libass and is not a silent fallback to one default font.
    cfg = load_config()
    a = style_preview.preview_png("clean-minimal", "Montserrat ExtraBold", cfg=cfg)
    b = style_preview.preview_png("clean-minimal", "Montserrat Black", cfg=cfg)
    assert a != b, "different fonts produced the same cache key"
    assert a.read_bytes() != b.read_bytes(), "different fonts rendered identically"


if __name__ == "__main__":
    test_list_fonts_reads_families()
    test_preview_png_caches()
    test_font_override_changes_render()
    print("ok")
