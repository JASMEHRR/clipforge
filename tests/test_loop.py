"""Content loop logic (loop_logic.py): pure, offline, no files."""

from datetime import datetime, timedelta

import loop_logic as L
from schemas import is_valid

NOW = datetime(2026, 11, 24, 21, 0, tzinfo=L.IST)


def post(platform, title, views, days_ago=2, hour=19, likes=0, shares=0, key="output/x/clip_00", topic=None):
    when = (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=5)
    return {"platform": platform, "key": key, "title": title, "views": views, "likes": likes, "shares": shares,
            "posted_at": when.isoformat(), **({"topic": topic} if topic else {})}


def test_format_and_topic_reading():
    assert L.fmt_of("output/_workspaces/money/2026_x/clip_01") == "explainer"
    assert L.fmt_of("output\\_workspaces\\trending\\x\\clip_00") == "story"
    assert L.fmt_of("output/20260712-171540_url/clip_09") == "clip"
    assert L.topic_of("Nvidia reinstates Jensen Huang as CEO") == "nvidia reinstates"
    assert L.topic_of("anything", "Home Loans") == "home loans"
    assert L.hour_ist("2026-07-13T15:07:29.987354+05:30") == 15
    assert L.hour_ist("2026-07-13T09:30:00Z") == 15  # 09:30 UTC is 3:00 PM IST
    assert L.hour_ist("not a date") is None


def test_results_from_muse_drop_bad_rows_and_bad_numbers():
    rows = L.from_posts([
        post("instagram", "A", 100),
        {"platform": "tiktok", "key": "k", "posted_at": NOW.isoformat()},
        {"key": "no platform"},
        {"platform": "linkedin", "key": "k2", "posted_at": NOW.isoformat(), "views": "oops", "likes": -4},
    ])
    assert [r["platform"] for r in rows] == ["instagram", "linkedin"]
    assert rows[1]["views"] == 0 and rows[1]["likes"] == 0


def test_learning_needs_enough_posts_and_finds_what_works():
    thin = L.from_posts([post("instagram", "Nvidia chips", 900), post("instagram", "Bank rates", 10)])
    assert L.learn(thin, NOW)["platforms"]["instagram"]["enough"] is False

    rows = L.from_posts([
        post("instagram", "x", 900, topic="nvidia", hour=19), post("instagram", "y", 1100, topic="nvidia", hour=19),
        post("instagram", "z", 40, topic="bank rates", hour=9), post("instagram", "w", 60, topic="bank rates", hour=9),
        post("instagram", "v", 300, topic="other", hour=13), post("instagram", "u", 280, topic="other", hour=13),
        post("instagram", "old hit", 99999, days_ago=60, topic="ancient"),
    ])
    s = L.learn(rows, NOW)["platforms"]["instagram"]
    assert s["enough"] is True and s["posts"] == 6, "results older than the lookback never count"
    assert [g["name"] for g in s["working_topics"]] == ["nvidia"]
    assert [g["name"] for g in s["weak_topics"]] == ["bank rates"]
    assert s["best_hours"][0] == 19


def test_brief_leans_on_what_worked_and_stays_neutral_without_data():
    trends = [{"title": "Bank raises rates again", "source": "gnews"}, {"title": "Nvidia ships a new chip", "source": "hn"},
              {"title": "A quiet day in space", "source": "reddit"}]
    neutral = L.make_brief({"platforms": {}}, trends, NOW, ideas=2)
    assert neutral["enough_data"] is False and [i["title"] for i in neutral["ideas"]] == [t["title"] for t in trends[:2]]
    assert L.prompt_note(neutral) == ""
    assert neutral["post_hours"] == L.DEFAULT_HOURS

    rows = L.from_posts([
        post("instagram", "x", 900, topic="nvidia chip"), post("instagram", "y", 1100, topic="nvidia chip"),
        post("instagram", "z", 40, topic="bank rates"), post("instagram", "w", 60, topic="bank rates"),
        post("instagram", "v", 300, topic="other"), post("instagram", "u", 280, topic="other"),
    ])
    brief = L.make_brief(L.learn(rows, NOW), trends, NOW, ideas=3)
    assert brief["ideas"][0]["title"] == "Nvidia ships a new chip"
    assert brief["ideas"][-1]["title"] == "Bank raises rates again", "a topic that flopped sinks"
    assert "nvidia chip" in L.prompt_note(brief) and "bank rates" in L.prompt_note(brief)
    assert brief["post_hours"]["instagram"] == [19]


def test_next_slot_is_future_ist_and_skips_taken():
    s1 = L.next_slot([20, 10], NOW, set())
    assert s1.hour == 10 and s1.date() == (NOW + timedelta(days=1)).date(), "9 PM now: today's 8 PM has passed"
    s2 = L.next_slot([20, 10], NOW, {s1.isoformat()})
    assert s2.hour == 20 and s2 > s1


def test_package_fits_every_platform_and_passes_its_schema():
    clip = {"key": "output/_workspaces/trending/2026_x/clip_00", "title": "Nvidia just did something odd",
            "description": "Two sentences about it.", "hashtags": [f"#tag{i}" for i in range(40)],
            "video": "E:/x/final.mp4", "approval": "pending",
            "social": {"instagram": {"caption": "Hook\n\n" + " ".join(f"#t{i}" for i in range(45))}}}
    taken: dict = {}
    p = L.package(clip, {"post_hours": {"youtube": [20], "instagram": [19], "linkedin": [9]}}, NOW, taken)
    assert len(L._tags(p["platforms"]["instagram"]["caption"])) <= L.IG_HASHTAGS_MAX
    assert len(L._tags(p["platforms"]["linkedin"]["text"])) <= L.LI_HASHTAGS
    assert p["format"] == "story" and p["status"] == "ready" and "/" not in p["id"]
    assert is_valid(p, "loop_post")
    second = L.package({**clip, "key": clip["key"] + "1"}, {"post_hours": {"youtube": [20], "instagram": [19], "linkedin": [9]}}, NOW, taken)
    assert second["platforms"]["instagram"]["post_at"] != p["platforms"]["instagram"]["post_at"]


def test_results_schema_rejects_garbage_from_outside():
    assert is_valid({"posts": [post("instagram", "A", 10)]}, "loop_results")
    assert not is_valid({"posts": [{"platform": "instagram"}]}, "loop_results")
    assert not is_valid({"posts": [{**post("instagram", "A", 10), "views": -1}]}, "loop_results")


def test_weekly_report_reads_plainly():
    rows = L.from_posts([post("youtube", "a", 100, days_ago=1), post("youtube", "b", 50, days_ago=9)])
    week = L.weekly(rows, NOW)
    assert week["youtube"]["this"]["views"] == 100 and week["youtube"]["last"]["views"] == 50
    text = L.report_md(week, {}, L.make_brief({"platforms": {}}, [], NOW))
    assert "Youtube: 1 posts, 100 views" in text and "Not enough results yet" in text
    assert "\u2014" not in text and "\u2013" not in text
