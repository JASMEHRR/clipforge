"""Pure-logic tests for style_profile helpers (no transcription)."""
import style_profile as sp


def test_collect_refs_expands_dir(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mov").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("skip me")
    refs = sp._collect_refs([str(tmp_path)])
    assert len(refs) == 2
    assert all(r.endswith((".mp4", ".mov")) for r in refs)


def test_collect_refs_passes_url_through():
    refs = sp._collect_refs(["https://youtu.be/abc123"])
    assert refs == ["https://youtu.be/abc123"]


def test_collect_refs_skips_missing():
    assert sp._collect_refs(["nope_does_not_exist.mp4"]) == []
