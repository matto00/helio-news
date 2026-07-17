"""Phase 1 — RSS/Atom ingestion.

Pulls every feed in config/outlets.yaml, normalises entries to `Article`, dedupes
by URL, and keeps only entries inside the configured lookback window. Deliberately
dumb: gathering is robust structured RSS; all interpretation happens later in the
gemma passes. `python -m news.fetch --check` validates feed URLs.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "outlets.yaml"


@dataclass
class Article:
    title: str
    url: str
    source: str
    topic: str                       # feed group it came from (grounding/politics/…)
    weight: float                    # topic weight from config
    published: datetime | None
    summary: str = ""                # raw RSS summary/description (may be HTML)
    matched: list[str] = field(default_factory=list)  # watchlist entities present
    image_url: str = ""              # lead image, if the feed carried one

    def as_row(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "topic": self.topic,
            "published": self.published.isoformat() if self.published else "",
            "summary": self.summary,
            "matched": ", ".join(self.matched),
        }


# ── lead images ───────────────────────────────────────────────────────────────
# Feeds advertise images four different ways, and roughly 2/3 of ours carry one
# at all (ESPN, Al Jazeera, TechCrunch and CNBC carry none) — which is why image
# selection happens per-STORY: a story with several sources usually has at least
# one illustrated member even when its lead article has no photo.
#
# Size is the whole game here. Feeds offer several renditions of the same photo
# and the first listed is usually the smallest: the Guardian advertises 140 /
# 460 / 700px in that order, so taking the first gets you a thumbnail stretched
# across half a dashboard. We take the WIDEST advertised variant instead.

_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)

# Below this the image is a thumbnail, not a photo — better no panel than a
# blurry one. The Guardian's smallest rendition is 140px, the BBC's is 240px.
MIN_IMAGE_WIDTH = 400

# Feeds whose only rendition is too small, but whose CDN serves a wider one at a
# predictable path. BBC ships 240px thumbnails and accepts /800/ (verified).
# NB: only safe for UNSIGNED urls — the Guardian's carry an `&s=` signature over
# the width, so their variants must be *selected*, never rewritten.
_CDN_UPGRADES = (
    (re.compile(r"(ichef\.bbci\.co\.uk/(?:ace|news)/)(?:standard|cpsprodpb)/\d+/"),
     r"\g<1>standard/800/"),
)

# Tracking pixels, share badges and social chrome — never a story's lead image.
_IMG_REJECT = re.compile(r"(doubleclick|pixel|1x1|spacer|avatar|logo|icon|badge"
                         r"|feedburner|gravatar|/rss/|button)", re.I)


def _upgrade_image(url: str) -> tuple[str, int]:
    """Rewrite known-tiny CDN renditions to a usable width. Returns (url, width)
    where width is the upgraded width when we rewrote, else 0 (= unknown)."""
    for pattern, repl in _CDN_UPGRADES:
        new = pattern.sub(repl, url)
        if new != url:
            return new, 800
    return url, 0


def _width_of(url: str, advertised: str | int | None) -> int:
    """Best guess at a candidate's pixel width: the feed's own attribute, else a
    `width=` query param, else 0 for "unknown" (we keep those — an <img> in the
    body is usually full size — but rank them below anything measurable)."""
    try:
        if advertised and int(advertised) > 0:
            return int(advertised)
    except (TypeError, ValueError):
        pass
    m = re.search(r"[?&]width=(\d+)", url)
    return int(m.group(1)) if m else 0


def _entry_image(entry) -> str:
    """Best-effort lead image for one feed entry ("" when the feed carries none
    or offers only thumbnails). Picks the widest usable rendition."""
    candidates: list[tuple[int, str]] = []      # (width, url)

    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            if isinstance(m, dict) and m.get("url"):
                url = m["url"].strip()
                candidates.append((_width_of(url, m.get("width")), url))

    for link in entry.get("links") or []:
        if (link.get("rel") == "enclosure"
                and str(link.get("type", "")).startswith("image")
                and link.get("href")):
            url = link["href"].strip()
            candidates.append((_width_of(url, None), url))

    # Last resort: the first <img> inside the entry body.
    for key in ("content", "summary"):
        val = entry.get(key)
        html = ""
        if isinstance(val, list) and val:
            html = val[0].get("value", "") if isinstance(val[0], dict) else ""
        elif isinstance(val, str):
            html = val
        m = _IMG_SRC_RE.search(html or "")
        if m:
            url = m.group(1).strip()
            candidates.append((_width_of(url, None), url))

    usable: list[tuple[int, str]] = []
    for width, url in candidates:
        if not url.startswith("http") or _IMG_REJECT.search(url):
            continue
        if width and width < MIN_IMAGE_WIDTH:
            url, upgraded = _upgrade_image(url)
            if not upgraded:
                continue          # too small and no upgrade path — skip it
            width = upgraded
        usable.append((width, url))

    if not usable:
        return ""
    # Widest wins; unknown widths (0) rank last but still beat nothing.
    return max(usable, key=lambda c: c[0])[1]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None) or entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def _watchlist_matches(text: str, watchlist: dict) -> list[str]:
    low = text.lower()
    hits: list[str] = []
    for bucket in ("companies", "people", "teams"):
        for ent in watchlist.get(bucket, []) or []:
            if any(kw.lower() in low for kw in ent.get("keywords", [])):
                hits.append(ent["name"])
    return hits


def fetch_all(config: dict | None = None) -> list[Article]:
    config = config or load_config()
    lookback = timedelta(hours=config.get("defaults", {}).get("lookback_hours", 30))
    cutoff = datetime.now(timezone.utc) - lookback
    watchlist = config.get("watchlist", {})

    seen: set[str] = set()
    articles: list[Article] = []
    for topic, group in config.get("feeds", {}).items():
        weight = float(group.get("weight", 1.0))
        for src in group.get("sources", []):
            parsed = feedparser.parse(src["url"])
            for e in parsed.entries:
                url = (e.get("link") or "").strip()
                if not url or url in seen:
                    continue
                published = _entry_time(e)
                if published and published < cutoff:
                    continue
                seen.add(url)
                title = (e.get("title") or "").strip()
                summary = (e.get("summary") or "").strip()
                articles.append(Article(
                    title=title,
                    url=url,
                    source=src["name"],
                    topic=topic,
                    weight=weight,
                    published=published,
                    summary=summary[:1000],
                    matched=_watchlist_matches(f"{title} {summary}", watchlist),
                    image_url=_entry_image(e),
                ))
    # Freshest first; unknown timestamps sink to the bottom.
    articles.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)
    return articles


def check_feeds(config: dict | None = None) -> int:
    """Validate every feed URL; returns count of problem feeds (for exit code)."""
    config = config or load_config()
    problems = 0
    for topic, group in config.get("feeds", {}).items():
        for src in group.get("sources", []):
            parsed = feedparser.parse(src["url"])
            n = len(parsed.entries)
            bozo = getattr(parsed, "bozo", 0)
            status = getattr(parsed, "status", "?")
            ok = n > 0 and not (bozo and n == 0)
            flag = "ok " if ok else "BAD"
            if not ok:
                problems += 1
            # Image coverage isn't a failure — several good feeds carry none —
            # but it's the thing that decides whether stories get hero images.
            imgs = sum(1 for e in parsed.entries[:10] if _entry_image(e))
            print(f"[{flag}] {topic:9} {src['name']:20} entries={n:<4} "
                  f"http={status:<4} img={imgs}/{min(10, n)}")
    print(f"\n{problems} feed(s) need attention." if problems else "\nAll feeds OK.")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch/validate news RSS feeds.")
    ap.add_argument("--check", action="store_true", help="validate feed URLs and exit")
    ap.add_argument("--limit", type=int, default=20, help="how many articles to print")
    args = ap.parse_args(argv)

    if args.check:
        return 1 if check_feeds() else 0

    arts = fetch_all()
    print(f"Fetched {len(arts)} fresh articles.\n")
    for a in arts[: args.limit]:
        tag = f" [{', '.join(a.matched)}]" if a.matched else ""
        print(f"· {a.source:18} {a.title[:80]}{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
