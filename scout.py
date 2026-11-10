"""Story scout for the funny-reel channel: finds today's most talked-about
tech / business / gaming stories for free and turns the best into reels.

  fetch    Reddit top-of-day Atom feeds, Hacker News front page (Algolia API),
           Google News topic RSS - no keys, no paid APIs (X/Twitter's API is
           paid, and viral tweets surface on Reddit within hours anyway)
  rank     pure: drop already-used and near-duplicate titles, interleave the
           sources so one feed cannot crowd out the rest
  pick     the LLM chooses the N stories with the most reel potential
           (surprising, funny, easy to explain) - schema `story_picks`
  read     best-effort fetch of each picked article's text = the ONLY
           background the script writer may use (no text -> title only)
  make     explain.make_story for each pick (script -> judge -> render)

CLI:
  .venv/Scripts/python.exe scout.py                 # list today's candidates
  .venv/Scripts/python.exe scout.py --make 2        # make the 2 best reels
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from config import ROOT, load_config
from errors import ClipForgeError, LLMError
from logutil import get_logger

log = get_logger("scout")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ClipForge-scout/1.0"
_ATOM = "{http://www.w3.org/2005/Atom}"


class ScoutError(ClipForgeError):
    stage = "scout"


def _scfg(cfg: dict) -> dict:
    s = cfg.get("scout")
    if not isinstance(s, dict):
        raise ScoutError("config is missing the scout: block", retryable=False)
    return s


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(2_000_000)


def strip_html(raw: str, limit: int = 1800) -> str:
    """Pure. Visible text of an HTML fragment/page: scripts, styles and tags
    removed, entities decoded, whitespace collapsed, cut at `limit` chars."""
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:limit]


# ------------------------------------------------------------------ sources

def parse_reddit(xml_bytes: bytes, sub: str) -> list[dict]:
    """Pure. Atom feed of r/<sub>/top -> candidates (feed order = rank)."""
    out = []
    for e in ET.fromstring(xml_bytes).findall(f"{_ATOM}entry"):
        title = (e.findtext(f"{_ATOM}title") or "").strip()
        content = e.findtext(f"{_ATOM}content") or ""
        link = e.find(f"{_ATOM}link")
        m = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
        if title:
            out.append({"id": (e.findtext(f"{_ATOM}id") or title)[-40:], "title": title,
                        "source": f"Reddit r/{sub}",
                        "url": html.unescape(m.group(1)) if m else
                        (link.get("href") if link is not None else ""),
                        "summary": ""})
    return out


def parse_hn(json_bytes: bytes) -> list[dict]:
    """Pure. Algolia front-page hits -> candidates, most points first."""
    hits = json.loads(json_bytes).get("hits", [])
    hits.sort(key=lambda h: -(h.get("points") or 0))
    return [{"id": f"hn{h.get('objectID')}", "title": h["title"].strip(),
             "source": "Hacker News", "url": h.get("url") or "", "summary": ""}
            for h in hits if h.get("title")]


def parse_gnews(xml_bytes: bytes, topic: str) -> list[dict]:
    """Pure. Google News RSS -> candidates. Titles end in ' - Publisher'."""
    out = []
    for it in ET.fromstring(xml_bytes).iter("item"):
        title = (it.findtext("title") or "").strip()
        if title:
            out.append({"id": (it.findtext("guid") or title)[-40:],
                        "title": re.sub(r"\s+-\s+[^-]+$", "", title),
                        "source": f"Google News ({topic.title()})", "url": "",
                        "summary": strip_html(it.findtext("description") or "", 300)})
    return out


def fetch_candidates(cfg: dict) -> list[list[dict]]:
    """One candidate list per source that answered (a dead source is logged
    and skipped - the scout works with whatever is up)."""
    s = _scfg(cfg)
    feeds: list[tuple[str, object]] = []
    for sub in s.get("subreddits") or []:
        feeds.append((f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit=10",
                      lambda b, sub=sub: parse_reddit(b, sub)))
    if s.get("hacker_news", True):
        feeds.append(("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20",
                      parse_hn))
    for topic in s.get("news_topics") or []:
        feeds.append((f"https://news.google.com/rss/headlines/section/topic/{topic}"
                      "?hl=en-US&gl=US&ceid=US:en", lambda b, topic=topic: parse_gnews(b, topic)))
    lists = []
    for url, parse in feeds:
        if "reddit.com" in url:
            time.sleep(2.5)              # Reddit answers 429 to back-to-back feed requests
        try:
            lists.append(parse(_get(url))[: int(s.get("per_source", 8))])
        except (OSError, ValueError, ET.ParseError, KeyError) as e:
            log.warning("source skipped (%s): %s", url.split("/")[2], e)
    return lists


# ------------------------------------------------------------------ rank

def _key(title: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3)


def rank(lists: list[list[dict]], used: list[str], limit: int) -> list[dict]:
    """Pure. Round-robin across the sources (each list is already best-first),
    skipping used ids and titles that share most of their words with one
    already kept (the same story from two sources)."""
    out: list[dict] = []
    keys: list[frozenset[str]] = []
    seen = set(used)
    for depth in range(max((len(x) for x in lists), default=0)):
        for src in lists:
            if depth >= len(src) or len(out) >= limit:
                continue
            c, k = src[depth], _key(src[depth]["title"])
            dup = any(k and len(k & o) / max(1, min(len(k), len(o))) >= 0.6 for o in keys)
            if c["id"] in seen or dup:
                continue
            seen.add(c["id"])
            keys.append(k)
            out.append(c)
    return out


def _used_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["cache_dir"] / "scout_used.json"


def load_used(cfg: dict) -> list[str]:
    try:
        data = json.loads(_used_path(cfg).read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def mark_used(ids: list[str], cfg: dict) -> None:
    p = _used_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps((load_used(cfg) + ids)[-2000:]), encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------ pick + read

def pick_prompt(cands: list[dict], n: int) -> str:
    rows = "\n".join(f"{i}. [{c['source']}] {c['title']}" + (f" - {c['summary']}" if c["summary"]
                                                            else "") for i, c in enumerate(cands))
    return (
        f"These are today's most discussed tech, business and gaming stories. Pick the {n} "
        "that would make the best 30-45 second funny commentary reel: something surprising, "
        "ironic or absurd that a casual viewer gets in one sentence (a company doing "
        "something odd, a wild number, a clever move, an embarrassing mistake). Prefer "
        "stories about companies, products and numbers. NEVER pick: tragedies, deaths, "
        "crime against people, war, elections or party politics, anyone's private life "
        "(affairs, scandals, health), or unproven allegations and lawsuits against a "
        "named person - a joke there would be cruel or defamatory.\n\n"
        f"{rows}\n\n"
        "Return picks: for each, index (the number above) and why (one sentence on what "
        "is funny or surprising about it). Best first. Return only JSON matching the schema."
    )


def pick(cands: list[dict], n: int, cfg: dict, provider: str | None) -> list[dict]:
    """The n candidates with the most reel potential (LLM); on mock or LLM
    failure the first n as ranked."""
    import create
    if provider != "mock":
        try:
            data = create.ask("story_picks", pick_prompt(cands, n), cfg, provider,
                              {"candidates": len(cands)})
            idx = [p["index"] for p in data["picks"] if 0 <= p["index"] < len(cands)]
            chosen = [cands[i] for i in dict.fromkeys(idx)][:n]
            if chosen:
                return chosen
        except LLMError as e:
            log.warning("picker failed (%s) - taking the top %d as ranked", e, n)
    return cands[:n]


def read_article(url: str) -> str:
    """Best-effort visible text of the linked article ('' when it can't be
    read: paywall, video, Reddit self-link...)."""
    if not url.startswith("http") or re.search(r"reddit\.com|redd\.it|youtu|\.(png|jpg|gif|mp4)$",
                                               url):
        return ""
    try:
        text = strip_html(_get(url, timeout=15).decode("utf-8", "replace"), 2200)
    except (OSError, ValueError) as e:
        log.info("article not readable (%s): %s", url[:60], e)
        return ""
    return text if len(text) > 300 else ""


def as_story(c: dict) -> tuple[dict, str]:
    """(post, context) for explain.make_story. The context is everything the
    writer may treat as fact."""
    article = read_article(c["url"])
    post = {"author": c["source"], "source": "trending today", "text": c["title"]}
    context = (f"Headline: {c['title']}\n" + (f"Summary: {c['summary']}\n" if c["summary"] else "")
               + (f"From the article: {article}" if article else
                  "No article text is available: use only what the headline itself states."))
    return post, context


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    import config as config_mod
    import explain
    import workspaces
    from ffutil import verify_ffmpeg
    ap = argparse.ArgumentParser(description="ClipForge story scout")
    ap.add_argument("--make", type=int, default=0, help="make this many reels from the best picks")
    ap.add_argument("--provider", help="LLM provider override (mock|gemini|...)")
    args = ap.parse_args(argv)
    cfg = load_config()
    s = _scfg(cfg)
    cands = rank(fetch_candidates(cfg), load_used(cfg), int(s.get("shortlist", 30)))
    if not cands:
        print("no candidates (all sources down or everything already used)")
        return 1
    if not args.make:
        for i, c in enumerate(cands):
            print(f"{i:2d}. [{c['source']}] {c['title']}")
        return 0
    e = cfg.get("explain") or {}
    ws = str(e.get("story_workspace", "default"))
    workspaces.ensure_workspace(ws, str(e.get("story_workspace_name", ws.title())))
    config_mod.current_workspace.set(ws)
    cfg = load_config()
    verify_ffmpeg(cfg)
    # ask for spares: a story whose script the judge rejects is replaced by the next
    chosen = pick(cands, args.make + int(s.get("spares", 2)), cfg, args.provider)
    made = 0
    for c in chosen:
        if made >= args.make:
            break
        post, context = as_story(c)
        job_dir = explain.make_story(cfg, post, context, args.provider)
        status = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"]
        mark_used([c["id"]], cfg)            # judged-out stories are not retried either
        print(f"{status}: {c['title'][:70]} -> {job_dir}")
        made += status == "done"
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
