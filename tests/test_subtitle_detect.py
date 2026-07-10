"""subtitle_detect against a synthetic ffmpeg-rendered clip (text-like band)."""
import subtitle_detect
from ffutil import probe


def test_detects_burned_band(tmp_path):
    vid = subtitle_detect.make_synthetic(tmp_path / "subs.mp4", with_subs=True)
    dur = probe(vid)["duration"]
    r = subtitle_detect._detect_uncached(vid, 0.0, dur, 2.0, 0.45, 0.30)
    assert r["present"] is True
    # Fake band sits ~0.84 of frame height; must land in the lower region.
    assert r["band_top_pct"] > 0.55
    assert r["band_bottom_pct"] <= 1.0
    assert r["band_bottom_pct"] > r["band_top_pct"]
    assert 0.0 < r["confidence"] <= 1.0
    assert r["sampled_frames"] > 0


def test_clean_clip_reports_none(tmp_path):
    vid = subtitle_detect.make_synthetic(tmp_path / "clean.mp4", with_subs=False)
    dur = probe(vid)["duration"]
    r = subtitle_detect._detect_uncached(vid, 0.0, dur, 2.0, 0.45, 0.30)
    assert r["present"] is False
    assert r["confidence"] == 0.0


def test_result_validates_against_schema(tmp_path, cfg):
    from schemas import validate
    vid = subtitle_detect.make_synthetic(tmp_path / "s.mp4", with_subs=True)
    dur = probe(vid)["duration"]
    r = subtitle_detect.detect_subtitles(vid, 0.0, dur, cfg=cfg)
    validate(r, "subtitle_detect_result")  # raises if malformed


# --- regression: double-caption bug (intermittent burned subs false negative) ----

def test_intermittent_band_needs_a_window_not_a_global_average(tmp_path):
    """4s-on/8s-off over 3 cycles (36s total) -> band on-screen ~33% of the
    clip. Averaged over the WHOLE range that's near/under persistence_ratio
    (0.30) and used to report present=False; a 6s window catches it because
    within an "on" window the band is on-screen the whole time."""
    vid = subtitle_detect.make_intermittent_synthetic(
        tmp_path / "intermittent.mp4", on_s=3.0, off_s=9.0, cycles=3)
    dur = probe(vid)["duration"]

    windowed = subtitle_detect._detect_uncached(vid, 0.0, dur, 2.0, 0.45, 0.30,
                                                window_seconds=6.0)
    assert windowed["present"] is True
    assert windowed["band_top_pct"] > 0.5

    # A window as long as the whole clip reproduces the old (buggy) global
    # average and must still fail to see the band — proves the fix is the
    # windowing, not a lowered threshold.
    global_avg = subtitle_detect._detect_uncached(vid, 0.0, dur, 2.0, 0.45, 0.30,
                                                  window_seconds=dur)
    assert global_avg["present"] is False


def test_single_one_off_burst_is_not_flagged(tmp_path):
    """A text-like band visible for one continuous stretch and never again
    (e.g. static signage briefly in frame, not a subtitle track) must NOT be
    reported present just because a window happened to land on it — that
    was a real regression found while fixing the intermittent-caption bug:
    windowing alone (no recurrence/continuity check) flagged a one-off prop
    as burned-in subtitles."""
    vid = subtitle_detect.make_intermittent_synthetic(
        tmp_path / "oneoff.mp4", on_s=6.0, off_s=30.0, cycles=1)
    dur = probe(vid)["duration"]
    r = subtitle_detect._detect_uncached(vid, 0.0, dur, 1.0, 0.45, 0.30,
                                         window_seconds=6.0)
    assert r["present"] is False


def test_intermittent_subs_drive_replace_not_none(tmp_path, cfg):
    """Full path: detection result feeds style_refiner's decision. Before the
    fix, this source would have been misdetected as present=False -> decision
    "none" -> new captions burned on top of the (still-visible) source
    subtitle -> double caption."""
    import style_refiner

    vid = subtitle_detect.make_intermittent_synthetic(
        tmp_path / "intermittent.mp4", on_s=4.0, off_s=8.0, cycles=3)
    dur = probe(vid)["duration"]
    subs = subtitle_detect.detect_subtitles(vid, 0.0, dur, cfg=cfg)
    assert subs["present"] is True

    decision, subs_kept, captions_enabled = style_refiner.decide_existing_subs(
        subs, cfg, mode=None)
    assert decision["decision"] in ("replace", "keep")


def test_verify_no_leftover_subs_flags_band_outside_own_caption_zone(tmp_path, cfg):
    """A final render with a text band sitting outside where ClipForge's own
    caption/CTA lives (e.g. a source subtitle the replace-crop failed to
    remove) must be flagged."""
    final = subtitle_detect.make_synthetic(tmp_path / "final.mp4", with_subs=True,
                                           seconds=3.0)
    # make_synthetic's fake band sits ~0.78-0.90; well outside a caption zone
    # anchored around 0.55-0.65.
    leftover = subtitle_detect.verify_no_leftover_subs(final, 0.44, 0.69, cfg=cfg)
    assert leftover is not None
    assert leftover["present"] is True


def test_verify_no_leftover_subs_ignores_own_caption_band(tmp_path, cfg):
    """A band that lands inside ClipForge's own caption/CTA zone is expected
    (it's our burned caption) and must NOT be flagged as a leftover."""
    final = subtitle_detect.make_synthetic(tmp_path / "final.mp4", with_subs=True,
                                           seconds=3.0)
    # Same fake band (~0.78-0.90) now declared as the caption zone itself.
    leftover = subtitle_detect.verify_no_leftover_subs(final, 0.70, 0.95, cfg=cfg)
    assert leftover is None


def test_verify_no_leftover_subs_clean_render(tmp_path, cfg):
    final = subtitle_detect.make_synthetic(tmp_path / "final.mp4", with_subs=False,
                                           seconds=3.0)
    assert subtitle_detect.verify_no_leftover_subs(final, 0.44, 0.69, cfg=cfg) is None
