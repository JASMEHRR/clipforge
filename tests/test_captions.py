from pathlib import Path

from captions import _font, _srt_ts, _ts, build_caption_lines, write_ass, write_srt


def _words(n=10, dt=0.4):
    return [{"word": f"word{i}", "start": round(i * dt, 3),
             "end": round(i * dt + 0.3, 3)} for i in range(n)]


def test_lines_grouped_max_words():
    lines = build_caption_lines(_words(10), max_words=3)
    assert [len(l["words"]) for l in lines] == [3, 3, 3, 1]


def test_lines_no_flicker_gap():
    lines = build_caption_lines(_words(6), max_words=3)
    # line 1 extends to line 2's start (no dead gap), capped at +0.6s
    assert lines[0]["end"] == lines[1]["start"]


def test_timestamps():
    assert _ts(0) == "0:00:00.00"
    assert _ts(3661.25) == "1:01:01.25"
    assert _srt_ts(1.5) == "00:00:01,500"


def test_font_mapping():
    assert _font("Montserrat Regular") == ("Montserrat", 0)
    assert _font("Montserrat Bold") == ("Montserrat", -1)
    assert _font("Montserrat ExtraBold") == ("Montserrat ExtraBold", 0)


def test_ass_all_presets(cfg, tmp_path):
    for preset in cfg["captions"]["presets"]:
        p = tmp_path / f"{preset}.ass"
        write_ass(_words(8), p, cfg, preset)
        text = p.read_text(encoding="utf-8-sig")
        assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
        assert f",{cfg['captions']['bottom_margin_px']}," in text  # safe margin
        assert "Dialogue:" in text
        if cfg["captions"]["presets"][preset].get("style") == "box":
            assert "BoxActive" in text
        if cfg["captions"]["presets"][preset].get("style") == "karaoke":
            assert r"\fscx" in text  # scale pop on active word


def test_ass_escapes_braces(cfg, tmp_path):
    words = [{"word": "he{llo}", "start": 0.0, "end": 0.4}]
    p = tmp_path / "esc.ass"
    write_ass(words, p, cfg, "karaoke-pop")
    body = p.read_text(encoding="utf-8-sig").split("[Events]")[1]
    assert "he{llo}" not in body and "he(llo)" in body


def test_srt_written(cfg, tmp_path):
    p = tmp_path / "t.srt"
    write_srt(_words(7), p, cfg)
    content = p.read_text(encoding="utf-8")
    assert "1\n" in content and "-->" in content
    assert content.count("-->") == 3  # ceil(7/3) lines


def test_uppercase_preset(cfg, tmp_path):
    p = tmp_path / "up.ass"
    write_ass([{"word": "hello", "start": 0.0, "end": 0.5}], p, cfg,
              "bold-impact")
    assert "HELLO" in p.read_text(encoding="utf-8-sig")
