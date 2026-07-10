"""Pure-HTML card builders behind the popup pickers (app.py)."""
import app


def test_aspect_cards_cover_all_values():
    for v in ("9:16", "1:1", "16:9"):
        html = app._aspect_card_html(v)
        assert v in html
        assert app._ASPECT_CARDS[v][0] in html


def test_subs_cards_plain_language():
    for v in ("auto", "replace", "keep", "ignore"):
        html = app._subs_card_html(v)
        assert app._SUBS_CARDS[v][0] in html
        assert app._SUBS_CARDS[v][1] in html


def test_wmpos_cards_position_dot():
    for v in ("top-left", "top-right", "bottom-left", "bottom-right", "center"):
        html = app._wmpos_card_html(v)
        assert app._WMPOS_CARDS[v] in html
        assert "border-radius:50%" in html          # the position dot


def test_profile_card_with_fields_and_empty():
    html = app._profile_card_html(
        "user", {"captions": {"preset": "bold", "font": "Impact"},
                 "pacing": {"target_wpm": 170}})
    assert "user" in html and "bold" in html and "Impact" in html and "170" in html
    empty = app._profile_card_html("default", {})
    assert "default" in empty and "Custom style" in empty


def test_resolve_music_choice(monkeypatch):
    import music
    monkeypatch.setattr(music, "list_tracks",
                        lambda: [{"id": "a"}, {"id": "b"}])
    assert app._resolve_music_choice("") == ""
    assert app._resolve_music_choice("auto") == "auto"
    assert app._resolve_music_choice("track7") == "track7"
    assert app._resolve_music_choice("random") in ("a", "b")
    monkeypatch.setattr(music, "list_tracks", lambda: [])
    assert app._resolve_music_choice("random") == ""   # empty manifest → off


def test_music_card_html():
    html = app._music_card_html(
        {"id": "t1", "title": "Sunny Days", "license": "CC-BY",
         "moods": ["happy"], "attribution_required": True})
    assert "Sunny Days" in html and "CC-BY" in html and "happy" in html
    assert "credit added" in html


def test_profile_card_escapes_html():
    html = app._profile_card_html("<x>", {"captions": {"preset": "<i>"}})
    assert "&lt;x&gt;" in html and "&lt;i&gt;" in html
    assert "<x>" not in html and "<i>" not in html
