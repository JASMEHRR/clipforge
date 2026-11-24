"""Pure logic for the content loop (loop.py does all the file and network work).

The loop: what was posted, and how it did on each platform -> what to make next
(a brief) -> videos -> one post package per video (YouTube Shorts, Instagram Reels,
LinkedIn) -> Muse plans the day and posts -> results come back -> the next brief.

Everything here is pure and deterministic, so tests run with no network, no LLM and
no files. Numbers are only ever learned from real results; with too little data the
brief says so instead of guessing.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
PLATFORMS = ("youtube", "instagram", "linkedin")
MIN_PER_GROUP = 2           # a topic, format or hour needs this many posts before it counts
LIFT_TO_COUNT = 0.25        # 25% above the platform's median before something is "working"
IG_CAPTION_MAX = 2200       # Instagram's caption limit
IG_HASHTAGS_MAX = 30        # Instagram's hashtag limit
LI_TEXT_MAX = 3000          # LinkedIn's post limit
LI_HASHTAGS = 3             # LinkedIn reads better with a few niche tags
DEFAULT_HOURS = {"youtube": [10, 15, 20], "instagram": [11, 19], "linkedin": [9, 18]}

_STOP = set("""a an and are as at be but by for from has have how i if in into is it its
just me my no not of on or our so than that the their them then there these they this
to too up us was we what when which who why will with you your new now one more most
over out after about can get got make made makes like after says said vs""".split())


# ---------------------------------------------------------------- reading results

def fmt_of(key: str) -> str:
    """Pure. The kind of video from its ClipForge output path."""
    k = (key or "").replace("\\", "/").lower()
    if "/_workspaces/money/" in k:
        return "explainer"
    if "/_workspaces/trending/" in k or "story" in k:
        return "story"
    if "/_workspaces/kids/" in k or "create" in k:
        return "kids"
    return "clip"


def topic_of(title: str, explicit: str | None = None) -> str:
    """Pure. A short topic label: the explicit one if given, else the first two
    meaningful words of the title ("nvidia ceo", "home loan")."""
    if explicit and explicit.strip():
        return explicit.strip().lower()[:40]
    words = [w for w in re.findall(r"[a-z0-9']+", (title or "").lower()) if w not in _STOP and len(w) > 2]
    return " ".join(words[:2]) or "other"


def hour_ist(iso: str | None) -> int | None:
    """Pure. The posting hour in IST, or None when the time is missing or broken."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).hour


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f >= 0 else 0.0  # NaN and negatives count as 0


def from_youtube(videos: list[dict]) -> list[dict]:
    """Pure. ClipForge's YouTube analytics cache rows -> loop result rows."""
    rows = []
    for v in videos or []:
        rows.append({
            "platform": "youtube", "key": v.get("key") or v.get("video_id") or "",
            "title": v.get("title") or "", "topic": topic_of(v.get("title") or "", v.get("source_name")),
            "format": fmt_of(v.get("key") or ""), "published_at": v.get("publish_at"),
            "hour": hour_ist(v.get("publish_at")), "views": _num(v.get("views")), "likes": _num(v.get("likes")),
            "comments": 0.0, "shares": 0.0, "saves": 0.0, "watch_pct": _num(v.get("avg_view_pct")),
        })
    return rows


def from_posts(posts: list[dict]) -> list[dict]:
    """Pure. Results Muse sends back for Instagram and LinkedIn -> loop result rows.
    Unknown platforms and rows without a platform are dropped, never guessed."""
    rows = []
    for p in posts or []:
        platform = str(p.get("platform") or "").lower()
        if platform not in PLATFORMS:
            continue
        rows.append({
            "platform": platform, "key": str(p.get("key") or p.get("id") or ""),
            "title": p.get("title") or "", "topic": topic_of(p.get("title") or "", p.get("topic")),
            "format": p.get("format") or fmt_of(str(p.get("key") or "")), "published_at": p.get("posted_at") or p.get("published_at"),
            "hour": hour_ist(p.get("posted_at") or p.get("published_at")), "views": _num(p.get("views")),
            "likes": _num(p.get("likes")), "comments": _num(p.get("comments")), "shares": _num(p.get("shares")),
            "saves": _num(p.get("saves")), "watch_pct": _num(p.get("watch_pct")),
        })
    return rows


def score(row: dict) -> float:
    """Pure. One number per post: reach plus weighted engagement (a share or save
    says far more than a view)."""
    return row["views"] + 5 * row["likes"] + 10 * row["comments"] + 15 * row["shares"] + 15 * row["saves"]


# ---------------------------------------------------------------- learning

def _within(rows: list[dict], now: datetime, days: int) -> list[dict]:
    cutoff = now - timedelta(days=days)
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r.get("published_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        if dt >= cutoff:
            out.append(r)
    return out


def _groups(rows: list[dict], field: str, base: float) -> list[dict]:
    groups: dict = {}
    for r in rows:
        if r.get(field) is None:
            continue
        groups.setdefault(r[field], []).append(r)
    out = []
    for name, members in groups.items():
        if len(members) < MIN_PER_GROUP:
            continue
        med = statistics.median(score(m) for m in members)
        out.append({"name": name, "posts": len(members), "median": round(med, 1), "lift": round(med / base - 1, 2)})
    out.sort(key=lambda g: g["lift"], reverse=True)
    return out


def learn(rows: list[dict], now: datetime, days: int = 28) -> dict:
    """Pure. What is working per platform over the last `days`. A group (topic,
    format, posting hour) needs MIN_PER_GROUP posts to count, and is "working"
    only at LIFT_TO_COUNT above that platform's median."""
    recent = _within(rows, now, days)
    out: dict = {"days": days, "posts": len(recent), "platforms": {}}
    for platform in PLATFORMS:
        mine = [r for r in recent if r["platform"] == platform]
        if len(mine) < MIN_PER_GROUP * 2:
            out["platforms"][platform] = {"posts": len(mine), "enough": False}
            continue
        base = statistics.median(score(r) for r in mine) or 1.0
        topics, formats, hours = _groups(mine, "topic", base), _groups(mine, "format", base), _groups(mine, "hour", base)
        ranked = sorted(mine, key=score, reverse=True)
        out["platforms"][platform] = {
            "posts": len(mine), "enough": True, "median_score": round(base, 1),
            "working_topics": [g for g in topics if g["lift"] >= LIFT_TO_COUNT],
            "weak_topics": [g for g in topics if g["lift"] <= -LIFT_TO_COUNT],
            "formats": formats,
            "best_hours": [g["name"] for g in hours if g["lift"] >= 0][:3],
            "top": [{"title": r["title"], "views": r["views"], "score": round(score(r), 1)} for r in ranked[:3]],
            "flops": [{"title": r["title"], "views": r["views"], "score": round(score(r), 1)} for r in ranked[-3:]],
        }
    return out


def weekly(rows: list[dict], now: datetime) -> dict:
    """Pure. This week against last week, per platform: posts, views, engagement."""
    def totals(rs: list[dict]) -> dict:
        return {"posts": len(rs), "views": int(sum(r["views"] for r in rs)),
                "engagement": int(sum(r["likes"] + r["comments"] + r["shares"] + r["saves"] for r in rs))}
    this_week = _within(rows, now, 7)
    last_two = _within(rows, now, 14)
    ids = {id(r) for r in this_week}
    last_week = [r for r in last_two if id(r) not in ids]
    return {p: {"this": totals([r for r in this_week if r["platform"] == p]),
                "last": totals([r for r in last_week if r["platform"] == p])} for p in PLATFORMS}


# ---------------------------------------------------------------- the brief

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def make_brief(signals: dict, trends: list[dict], now: datetime, ideas: int = 3) -> dict:
    """Pure. What to make next. Trends come from scout (title, source, summary);
    the learned topics tilt the ranking toward what worked and away from what flopped.
    With too little data the brief says so and keeps scout's own order."""
    lean, avoid, formats, hours = [], [], [], {}
    for platform, s in signals.get("platforms", {}).items():
        if not s.get("enough"):
            continue
        lean += [g["name"] for g in s["working_topics"]]
        avoid += [g["name"] for g in s["weak_topics"]]
        formats += [(g["name"], g["lift"], platform) for g in s["formats"]]
        if s["best_hours"]:
            hours[platform] = s["best_hours"]
    lean = list(dict.fromkeys(lean))[:6]
    avoid = [t for t in dict.fromkeys(avoid) if t not in lean][:6]
    lean_words = set().union(*[_words(t) for t in lean]) if lean else set()
    avoid_words = set().union(*[_words(t) for t in avoid]) if avoid else set()

    ranked = []
    for i, t in enumerate(trends or []):
        w = _words(f"{t.get('title', '')} {t.get('summary', '')}")
        bonus = len(w & lean_words) * 2 - len(w & avoid_words) * 3
        ranked.append((bonus, -i, t, sorted(w & lean_words), sorted(w & avoid_words)))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)

    picks = []
    for bonus, _neg_i, t, hits, misses in ranked[:ideas]:
        if hits:
            why = f"Close to what worked lately ({', '.join(hits[:3])})."
        elif misses:
            why = f"Trending, though similar topics did poorly ({', '.join(misses[:3])})."
        else:
            why = f"Trending now on {t.get('source', 'the web')}."
        picks.append({"title": t.get("title", ""), "source": t.get("source", ""), "why": why,
                      "format": "story", "platforms": list(PLATFORMS)})

    best_format = max(formats, key=lambda f: f[1])[0] if formats else None
    enough = any(s.get("enough") for s in signals.get("platforms", {}).values())
    return {
        "made_at": now.astimezone(IST).isoformat(timespec="minutes"),
        "enough_data": enough,
        "lean_toward": lean, "avoid": avoid, "best_format": best_format,
        "post_hours": {p: hours.get(p, DEFAULT_HOURS[p]) for p in PLATFORMS},
        "hours_learned": sorted(hours),
        "ideas": picks,
    }


def prompt_note(brief: dict | None) -> str:
    """Pure. The line scout's picker reads, so the LLM leans on real results. Empty
    when the brief has nothing learned, so a thin week never steers the picks."""
    if not brief or not brief.get("enough_data"):
        return ""
    parts = []
    if brief.get("lean_toward"):
        parts.append("Recent posts about " + ", ".join(brief["lean_toward"][:4]) + " did best: prefer stories close to these.")
    if brief.get("avoid"):
        parts.append("Posts about " + ", ".join(brief["avoid"][:4]) + " did worst: skip these unless the story is exceptional.")
    return " ".join(parts)


# ---------------------------------------------------------------- packages

def _tags(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"#[A-Za-z0-9_]+", text or "")))


def next_slot(hours: list[int], now: datetime, taken: set[str]) -> datetime:
    """Pure. The next free slot at one of `hours` (IST), never in the past, never a
    slot already in `taken` (ISO strings). Searches up to 14 days ahead."""
    base = now.astimezone(IST)
    for day in range(15):
        d = (base + timedelta(days=day)).date()
        for h in sorted(set(int(x) for x in hours if 0 <= int(x) <= 23)):
            slot = datetime(d.year, d.month, d.day, h, 0, tzinfo=IST)
            if slot > base and slot.isoformat() not in taken:
                return slot
    return base + timedelta(days=15)


def package(clip: dict, brief: dict, now: datetime, taken: dict[str, set[str]]) -> dict:
    """Pure. One post package for one video: text for each platform within its
    limits, and a suggested time per platform from the learned hours. `clip` has
    key, title, description, hashtags, video, approval, and optional social text
    from explain.py's social.json. `taken` is updated with the slots used."""
    social = clip.get("social") or {}
    title = (clip.get("title") or "").strip()
    desc = (clip.get("description") or "").strip()
    tags = _tags(" ".join(clip.get("hashtags") or [])) or _tags(social.get("instagram", {}).get("caption", ""))
    hours = brief.get("post_hours") or DEFAULT_HOURS

    yt = social.get("youtube_shorts") or {}
    yt_desc = yt.get("description") or f"{desc}\n\n{' '.join(tags[:8] + ['#shorts'])}".strip()

    ig_caption = (social.get("instagram") or {}).get("caption") or f"{title}\n\n{desc}\n\n{' '.join(tags)}".strip()
    ig_tags = _tags(ig_caption)
    if len(ig_tags) > IG_HASHTAGS_MAX:
        for t in ig_tags[IG_HASHTAGS_MAX:]:
            ig_caption = ig_caption.replace(t, "")
        ig_caption = re.sub(r"[ \t]{2,}", " ", ig_caption).strip()
    ig_caption = ig_caption[:IG_CAPTION_MAX]

    li_body = desc or title
    li_head = "" if li_body.lower().startswith(title.lower()) else f"{title}\n\n"  # never say the title twice
    li_text = f"{li_head}{li_body}\n\n{' '.join(tags[:LI_HASHTAGS])}".strip()[:LI_TEXT_MAX]

    slots = {}
    for p in PLATFORMS:
        used = taken.setdefault(p, set())
        slot = next_slot(hours.get(p) or DEFAULT_HOURS[p], now, used)
        used.add(slot.isoformat())
        slots[p] = slot.isoformat()

    return {
        "id": clip["key"].replace("\\", "/").strip("/").replace("/", "__"),
        "key": clip["key"], "video": clip.get("video", ""), "title": title,
        "format": fmt_of(clip["key"]), "approval": clip.get("approval", "pending"),
        "made_at": now.astimezone(IST).isoformat(timespec="minutes"),
        "platforms": {
            "youtube": {"title": (yt.get("title") or title)[:90], "description": yt_desc, "post_at": slots["youtube"]},
            "instagram": {"caption": ig_caption, "post_at": slots["instagram"]},
            "linkedin": {"text": li_text, "post_at": slots["linkedin"]},
        },
        "status": "ready",
    }


def report_md(week: dict, signals: dict, brief: dict) -> str:
    """Pure. The weekly note, written for a phone screen."""
    lines = ["# Content loop: this week", ""]
    for p in PLATFORMS:
        t, l = week[p]["this"], week[p]["last"]
        if t["posts"] == 0 and l["posts"] == 0:
            lines.append(f"- {p.title()}: nothing posted in the last two weeks.")
            continue
        lines.append(f"- {p.title()}: {t['posts']} posts, {t['views']} views, {t['engagement']} likes/comments/shares/saves "
                     f"(last week: {l['posts']} posts, {l['views']} views).")
    lines += ["", "## What the next videos lean on"]
    if not brief.get("enough_data"):
        lines.append("Not enough results yet to learn from. The loop keeps scout's own picks until each platform has a few posts.")
    else:
        if brief["lean_toward"]:
            lines.append("Working: " + ", ".join(brief["lean_toward"]) + ".")
        if brief["avoid"]:
            lines.append("Not working: " + ", ".join(brief["avoid"]) + ".")
        if brief.get("best_format"):
            lines.append(f"Best format so far: {brief['best_format']}.")
    lines += ["", "## Next ideas"]
    lines += [f"{i}. {idea['title']} ({idea['why']})" for i, idea in enumerate(brief.get("ideas", []), 1)] or ["None: no trends were reachable."]
    learned = ", ".join(brief.get("hours_learned") or []) or "none yet"
    lines += ["", f"Posting hours learned from results on: {learned}. Others use defaults."]
    return "\n".join(lines) + "\n"
