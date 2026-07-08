"""Virality v2 engagement-signals composition + bucketing (overnight run)."""
import virality as v
from schemas import validate


def _feat(**over):
    base = {"hook": "the shocking secret nobody warns you about",
            "text": "here is the shocking secret that will change everything today",
            "duration": 45.0, "weak_hook": False,
            "self_contained": True, "ending_complete": True,
            "words_per_sec": 2.6, "cuts_per_min": 18.0,
            "prof_wps": 2.8, "prof_cpm": 18.0,
            "words_per_line": 3, "prof_words_per_line": 3,
            "caption_coverage": 0.9, "emphasis_present": True,
            "captions_enabled": True, "energy_variance": 0.7}
    base.update(over)
    return base


def test_all_subscores_present_and_bounded():
    r = v.engagement_signals(_feat())
    assert len(r["signals"]) == 6
    for s in r["signals"]:
        assert 0.0 <= s["score"] <= 10.0
    assert 0 <= r["score"] <= 100
    validate(r, "virality")


def test_bands_map_to_score():
    assert v._band(80) == "Strong"
    assert v._band(50) == "Promising"
    assert v._band(20) == "Weak"
    strong = v.engagement_signals(_feat())
    weak = v.engagement_signals(_feat(
        hook="um so yeah", text="um so yeah", weak_hook=True,
        self_contained=False, ending_complete=False, duration=6.0,
        words_per_sec=0.4, caption_coverage=0.1, emphasis_present=False,
        energy_variance=0.05))
    assert strong["score"] > weak["score"]
    assert strong["band"] == "Strong"
    assert weak["band"] == "Weak"


def test_weak_hook_flag_lowers_hook_score():
    good = v.score_hook("shocking secret warning", "", False, True)[0]
    flagged = v.score_hook("shocking secret warning", "", True, True)[0]
    assert flagged < good


def test_completeness_neutral_when_style_off():
    s, reason = v.score_completeness(None, None)
    assert s == 5.0 and "style off" in reason


def test_duration_sweet_spot_peaks_midrange():
    assert v.score_duration(45.0)[0] > v.score_duration(8.0)[0]
    assert v.score_duration(45.0)[0] > v.score_duration(120.0)[0]


def test_verdict_thresholds_unchanged():
    # keep-logic-compatible verdict banding (70/40) preserved from v1
    assert v._verdict(75) == "post"
    assert v._verdict(50) == "maybe"
    assert v._verdict(10) == "skip"


def test_rate_virality_mock_is_deterministic_and_keyless(cfg):
    a = v.rate_virality("shocking secret that changes everything", "shocking secret",
                        45.0, cfg, provider="mock")
    b = v.rate_virality("shocking secret that changes everything", "shocking secret",
                        45.0, cfg, provider="mock")
    assert a == b
    assert "signals" in a and a["band"] in ("Strong", "Promising", "Weak")
    assert not any(s["name"] == "llm_rubric" for s in a["signals"])


if __name__ == "__main__":
    from config import load_config
    c = load_config()
    test_all_subscores_present_and_bounded()
    test_bands_map_to_score()
    test_weak_hook_flag_lowers_hook_score()
    test_completeness_neutral_when_style_off()
    test_duration_sweet_spot_peaks_midrange()
    test_verdict_thresholds_unchanged()
    test_rate_virality_mock_is_deterministic_and_keyless(c)
    print("ok")
