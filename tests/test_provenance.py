"""Clip source provenance: timestamp formatting and card source line."""
from app import _fmt_ts, _source_html


def test_fmt_ts_minutes_and_hours():
    assert _fmt_ts(0) == "00:00"
    assert _fmt_ts(201) == "03:21"          # 3:21
    assert _fmt_ts(3661) == "1:01:01"       # crosses an hour → hh:mm:ss
    assert _fmt_ts(-5) == "00:00"           # clamped


def test_source_html_present():
    html = _source_html({"original_source_start_s": 201.0,
                         "original_source_end_s": 233.0,
                         "source_name": "talk.mp4"})
    assert "Source: 03:21–03:53" in html and "talk.mp4" in html


def test_source_html_truncates_long_name():
    html = _source_html({"original_source_start_s": 0, "original_source_end_s": 5,
                        "source_name": "a" * 60})
    assert "…" in html


def test_source_html_absent_for_old_jobs():
    # Records rendered before provenance existed have no bounds → no line.
    assert _source_html({"duration": 30}) == ""
    assert _source_html({"original_source_start_s": 1}) == ""   # partial → skip


if __name__ == "__main__":
    test_fmt_ts_minutes_and_hours()
    test_source_html_present()
    test_source_html_truncates_long_name()
    test_source_html_absent_for_old_jobs()
    print("ok")
