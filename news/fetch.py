"""Phase 1 — RSS/Atom ingestion.

Pulls every feed in config/outlets.yaml, normalises entries to `Article`, dedupes
by URL, and keeps only entries inside the configured lookback window. Deliberately
dumb: gathering is robust structured RSS; all interpretation happens later in the
gemma passes. `python -m news.fetch --check` validates feed URLs.
"""

from __future__ import annotations

import argparse
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
            print(f"[{flag}] {topic:9} {src['name']:20} entries={n:<4} http={status}")
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
