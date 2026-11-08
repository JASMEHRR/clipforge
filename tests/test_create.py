"""Create mode: pure-logic checks only (no network, no ffmpeg, no TTS)."""
import random

import pytest

import create
import footage
import llm
import upload_scheduler as sched
import youtube_upload as yt
from schemas import validate


def _sections():
    return [
        {"key": "intro", "title": "Intro", "lines": [{"text": "hi", "visual": "a"}]},
        {"key": "ch1", "title": "One", "lines": [{"text": "q?", "visual": "b"},
                                                 {"text": "x", "visual": "c"}]},
        {"key": "outro", "title": "Recap", "lines": [{"text": "bye", "visual": "d"}]},
    ]


def test_timeline_gaps_and_sections():
    tl = create.build_timeline(_sections(), [1.0, 2.0, 3.0, 1.0],
                               line_gap=0.25, section_gap=0.7, tail=0.5)
    starts = [ln["start"] for ln in tl["lines"]]
    assert starts == [0.0, 1.7, 3.95, 7.65]       # section gap, line gap, section gap
    assert [s["key"] for s in tl["sections"]] == ["intro", "ch1", "outro"]
    ch = tl["sections"][1]
    assert (ch["first"], ch["last"], ch["start"], ch["end"]) == (1, 2, 1.7, 6.95)
    assert tl["total"] == pytest.approx(9.15)


def test_timeline_rejects_mismatch_and_bad_durations():
    with pytest.raises(create.CreateError):
        create.build_timeline(_sections(), [1.0], 0.25, 0.7)
    with pytest.raises(create.CreateError):
        create.build_timeline(_sections(), [1.0, 0.0, 1.0, 1.0], 0.25, 0.7)


def test_shot_frames_do_not_drift():
    bounds = [0.0] + [i * 1.017 for i in range(1, 101)]
    frames = create.shot_frames(bounds, 30)
    assert sum(frames) == round(bounds[-1] * 30)  # cumulative, not per-shot rounding
    with pytest.raises(create.CreateError):
        create.shot_frames([0.0, 0.001], 30)


def test_chapter_timestamps_start_at_zero():
    tl = create.build_timeline(_sections(), [12.0, 15.0, 15.0, 5.0], 0.25, 0.7)
    lines = create.chapter_timestamps(tl).splitlines()
    assert lines[0] == "0:00 Intro"
    assert lines[1] == "0:12 One"


def test_slice_words_retimes():
    words = [{"word": "a", "start": 0.5, "end": 0.9},
             {"word": "b", "start": 2.0, "end": 2.4},
             {"word": "c", "start": 5.0, "end": 5.5}]
    assert create.slice_words(words, 1.9, 3.0) == [{"word": "b", "start": 0.1,
                                                    "end": 0.5}]


def test_apply_fact_check():
    ch = {"title": "T", "lines": [{"text": "a", "visual": "v1"},
                                  {"text": "b", "visual": "v2"}], "facts": ["f"]}
    assert create.apply_fact_check(ch, {"verdict": "pass", "issues": []}) is ch
    fixed = create.apply_fact_check(ch, {"verdict": "fix", "issues": ["x"],
                                         "corrected_lines": ["A", "B"]})
    assert [ln["text"] for ln in fixed["lines"]] == ["A", "B"]
    assert [ln["visual"] for ln in fixed["lines"]] == ["v1", "v2"]
    assert create.apply_fact_check(ch, {"verdict": "fix", "issues": [],
                                        "corrected_lines": ["only one"]}) is None
    assert create.apply_fact_check(ch, {"verdict": "reject", "issues": []}) is None


def test_pick_topic_prefers_unused_then_restarts():
    rng = random.Random(0)
    assert create.pick_topic(["a", "b"], ["a"], rng) == "b"
    assert create.pick_topic(["a", "b"], ["a", "b"], rng) in {"a", "b"}
    assert create.pick_topic(["a", "b"], [], rng, exclude={"a"}) == "b"
    with pytest.raises(create.CreateError):
        create.pick_topic([], [], rng)


def test_mock_script_is_schema_valid():
    data = llm._mock_kids_long_script("the planet Saturn", 6)
    validate(data, "kids_long_script")
    assert len(data["chapters"]) == 6
    assert all(ch["lines"][0]["text"].endswith("?") for ch in data["chapters"])


def test_pick_pexels_prefers_orientation_and_length():
    data = {"videos": [
        {"id": 1, "duration": 3, "url": "p1", "user": {"name": "A"},
         "video_files": [{"file_type": "video/mp4", "link": "l1",
                          "width": 1920, "height": 1080}]},
        {"id": 2, "duration": 20, "url": "p2", "user": {"name": "B"},
         "video_files": [{"file_type": "video/mp4", "link": "l2",
                          "width": 1920, "height": 1080},
                         {"file_type": "video/mp4", "link": "small",
                          "width": 640, "height": 360}]},
        {"id": 3, "duration": 20, "url": "p3", "user": {"name": "C"},
         "video_files": [{"file_type": "video/mp4", "link": "l3",
                          "width": 1080, "height": 1920}]},
    ]}
    best = footage.pick_pexels(data, need_s=6, min_short=720, used=set())
    assert best["id"] == "pexels:2" and best["url"] == "l2"
    best = footage.pick_pexels(data, need_s=6, min_short=720, used=set(),
                               portrait=True)
    assert best["id"] == "pexels:3"
    assert footage.pick_pexels(data, 6, 720, {"pexels:2", "pexels:1",
                                              "pexels:3"}) is None


def test_pick_pixabay_respects_size_cap():
    data = {"hits": [{"id": 9, "pageURL": "pg", "duration": 10, "user": "u",
                      "videos": {"large": {"url": "big", "width": 1920,
                                           "height": 1080, "size": 999_999_999},
                                 "medium": {"url": "med", "width": 1280,
                                            "height": 720, "size": 5_000_000}}}]}
    best = footage.pick_pixabay(data, 6, 720, 60 * 1024 * 1024, set())
    assert best["url"] == "med"


def test_pick_nasa_needs_query_word():
    data = {"collection": {"items": [
        {"data": [{"nasa_id": "G1", "title": "Spiral Galaxy", "media_type": "image"}]},
        {"data": [{"nasa_id": "S1", "title": "Saturn Rings", "media_type": "image",
                   "center": "JPL"}]}]}}
    assert footage.pick_nasa(data, "saturn rings close", set())["nasa_id"] == "S1"
    assert footage.pick_nasa(data, "octopus coral", set()) is None


def test_upload_flags_for_long_and_kids():
    assert sched.clean_hashtags(["#cats"], shorts=False) == ["#cats"]
    assert sched.clean_hashtags(["#cats"]) == ["#cats", "#shorts"]
    snip = sched.build_snippet({"title": "T", "description": "D",
                                "hashtags": ["#saturn"], "format": "long",
                                "made_for_kids": True})
    assert "#shorts" not in snip["hashtags"] and snip["made_for_kids"] is True
    body = yt.build_request_body(snip)
    assert body["status"]["selfDeclaredMadeForKids"] is True
    assert yt.build_request_body({"title": "T", "description": "D",
                                  "hashtags": []})["status"][
        "selfDeclaredMadeForKids"] is False


# ---------------------------------------------------------------- animated mode

import numpy as np  # noqa: E402

import animate  # noqa: E402


def test_build_scenes_banners_and_order():
    sections = [{"key": "intro", "title": "Intro", "scene": "sky",
                 "lines": [{"text": "hi", "icons": ["👋"], "visual": "a"},
                           {"text": "go", "icons": ["🚀"], "visual": "b"}]},
                {"key": "ch1", "title": "Rings", "scene": "space",
                 "lines": [{"text": "q?", "icons": ["🪐"], "visual": "c"}]},
                {"key": "outro", "title": "Recap", "scene": "sky",
                 "lines": [{"text": "bye", "icons": ["👋"], "visual": "d"}]}]
    sc = create.build_scenes(sections, "Saturn!")
    assert [s["scene"] for s in sc] == ["sky", "sky", "space", "sky"]
    assert sc[0]["banner"] == {"kind": "title", "text": "Saturn!", "label": None}
    assert sc[1]["banner"] is None
    assert sc[2]["banner"] == {"kind": "chapter", "text": "Rings", "label": "Chapter 1"}
    assert sc[3]["banner"] is None and sc[2]["icons"] == ["🪐"]


def test_emoji_names_and_files():
    assert animate.emoji_filename("🐙") == "emoji_u1f419.png"
    assert animate.emoji_filename("❤️") == "emoji_u2764.png"            # FE0F dropped
    assert animate.emoji_filename("🧑‍🔬") == "emoji_u1f9d1_200d_1f52c.png"  # ZWJ
    assert animate.resolve_emoji("octopus") == "🐙"                     # name lookup
    assert animate.resolve_emoji("not an emoji name") is None
    assert animate.resolve_emoji("  ") is None


def test_mouth_levels_follow_loudness():
    sr, fps = 24000, 30
    t = np.arange(sr * 2) / sr
    sig = np.where(t < 1.0, 0.0, 0.5 * np.sin(2 * np.pi * 220 * t))   # 1s silence, 1s tone
    lv = animate.mouth_levels(sig, sr, np.arange(60) / fps, fps)
    assert lv[:25].max() == 0                                          # silent -> closed
    assert lv[35:].min() > 0 and lv.max() <= animate.MOUTH_LEVELS


def test_blink_states_are_short_and_sparse():
    eyes = animate.blink_states(900, 30, seed=3)
    assert set(np.unique(eyes)) <= {0, 1, 2}
    assert (eyes > 0).mean() < 0.12                                    # eyes mostly open
    assert (animate.blink_states(900, 30, seed=3) == eyes).all()       # deterministic


def test_frame_marks_and_layout():
    assert animate.frame_marks([2.0, 3.0, 4.5], 30) == [0, 30, 75]
    with pytest.raises(animate.AnimateError):
        animate.frame_marks([1.0, 1.001], 30)
    for w, h in ((1920, 1080), (1080, 1920)):
        lay = animate.layout(w, h)
        x0, y0, x1, y1 = lay["icons"]
        for n in (1, 2, 3):
            slots = animate.icon_slots(n, lay["icons"])
            assert len(slots) == n
            for cx, cy, s in slots:                                     # inside the area
                assert x0 <= cx - s / 2 and cx + s / 2 <= x1 + 1
                assert y0 <= cy - s / 2 + 1 and cy + s / 2 <= y1 + 1


def test_easing_endpoints_and_blit_clips():
    assert animate.ease_out_back(0) == pytest.approx(0)
    assert animate.ease_out_back(1) == pytest.approx(1)
    assert max(animate.ease_out_back(x / 20) for x in range(21)) > 1.0  # overshoot = pop
    dst = np.zeros((10, 10, 3), np.uint8)
    spr = np.full((6, 6, 4), 255, np.uint8)                            # opaque white
    animate.blit(dst, spr, 0, 0)                                       # 3/4 off-screen
    assert dst[:3, :3].min() == 255 and dst[5:, 5:].max() == 0


def test_create_clips_always_need_explicit_approval():
    cfg_off = {"upload": {"require_approval": False}}
    kids = {"upload": {"require_approval": True}}
    assert sched.approval_ok(kids, cfg_off) is False                     # pending -> blocked
    assert sched.approval_ok({"upload": {"require_approval": True,
                                         "approval": "approved"}}, cfg_off) is True
    assert sched.approval_ok({}, cfg_off) is True                        # other clips unchanged


def test_banner_text_drops_glyphs_the_font_lacks():
    assert animate.banner_text("Busy Bees \U0001F41D!") == "Busy Bees !"
    assert animate.banner_text("  Café   & Crème ") == "Café & Crème"
    assert animate.banner_text(None) == ""


def test_fact_check_fix_must_fit_the_schema():
    ch = {"title": "T", "lines": [{"text": "a", "icons": ["⭐"], "visual": "v"}],
          "facts": ["f"]}
    too_long = "x" * 221
    assert create.apply_fact_check(ch, {"verdict": "fix", "issues": [],
                                        "corrected_lines": [too_long]}) is None


# ---------------------------------------------------------------- illustrated mode

import art  # noqa: E402


def _tl_lines(spec):
    """spec = [(section, duration)] -> timeline-style lines with 0.25 s gaps."""
    out, t = [], 0.0
    for i, (sec, d) in enumerate(spec):
        out.append({"section": sec, "visual": f"v{i}", "start": t, "end": t + d})
        t += d + 0.25
    return out


def test_plan_beats_groups_lines_and_never_crosses_sections():
    lines = _tl_lines([(0, 3), (0, 3), (0, 3), (0, 3), (1, 2), (1, 9), (1, 2)])
    beats = art.plan_beats(lines, 8.0)
    assert [(b["first"], b["last"]) for b in beats] == [(0, 2), (3, 3), (4, 5), (6, 6)]
    assert beats[2]["visual"] == "v4" and beats[2]["section"] == 1
    covered = [i for b in beats for i in range(b["first"], b["last"] + 1)]
    assert covered == list(range(len(lines)))                      # every line once
    with pytest.raises(art.ArtError):
        art.plan_beats(lines, 0)


def test_art_prompt_and_cache_key():
    p = art.art_prompt(" a smiling octopus.", "ocean", "cartoon style")
    assert p == "a smiling octopus, underwater in the bright blue ocean, cartoon style"
    assert art.art_prompt("bee", "unknown-scene", "") == "bee"
    acfg = {"model": "m", "steps": 6, "guidance": 1.0, "width": 768, "height": 432}
    assert art.image_key(p, 1, acfg) == art.image_key(p, 1, dict(acfg))
    assert art.image_key(p, 1, acfg) != art.image_key(p, 2, acfg)
    assert art.image_key(p, 1, acfg) != art.image_key(p, 1, {**acfg, "steps": 8})


def test_split_layers_cuts_the_subject_and_paints_it_out():
    rgb = np.full((96, 160, 3), (40, 120, 220), np.uint8)          # blue sky
    rgb[30:70, 60:100] = (250, 140, 20)                             # orange subject
    alpha = np.zeros((96, 160), np.float32)
    alpha[30:70, 60:100] = 1.0
    alpha[2:4, 2:4] = 1.0                                           # speck: dropped
    fg, box, bg = art.split_layers(rgb, alpha)
    x, y, w, h = box
    assert x <= 60 and y <= 30 and x + w >= 100 and y + h >= 70 and w < 60
    assert fg.shape == (h, w, 4) and fg[..., 3].max() == 255
    assert abs(int(bg[50, 80, 2]) - 220) < 40                       # subject painted out
    none_fg, none_box, same = art.split_layers(rgb, np.zeros_like(alpha))
    assert none_fg is None and none_box is None and same is rgb     # no subject


def test_camera_moves_are_eased_and_bounded():
    for move in animate.CAMERA_MOVES:
        for p in (0.0, 0.3, 0.7, 1.0):
            z, fx, fy = animate.camera(move, p)
            assert 1.0 <= z <= 1 + animate.CAMERA_ZOOM + 1e-9
            assert -1.0 <= fx <= 1.0 and -1.0 <= fy <= 1.0
    assert animate.camera("zoom_in", 0)[0] == 1.0
    assert animate.camera("pan_right", 0)[1] == -animate.camera("pan_right", 1)[1]
    with pytest.raises(animate.AnimateError):
        animate.camera("spin", 0.5)


def test_shot_spans_follow_scene_bounds():
    scenes = [{"art": {"shot": s}} for s in (0, 0, 1, 2, 2)]
    spans = animate.shot_spans(scenes, [0, 1, 2, 4, 5, 7])
    assert spans == {0: (0, 2, 0), 1: (2, 4, 2), 2: (4, 7, 3)}
