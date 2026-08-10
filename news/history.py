"""Persistent headline memory — one JSON file per day, filtered at read time
into day/week/month buckets (no separate rollup storage). Pure/deterministic:
no model calls anywhere in this module. Matching (news.history.find_candidates)
and the day-count/trend arithmetic that grounds the historian/verifier passes
in news.agents both live here, so the honesty-critical numbers are never left
to a model to assert — see docs/superpowers/specs/2026-08-09-headline-history-design.md.

Only written on real runs (news/run.py gates this on `not args.plan_only`) —
a dev `--plan-only` loop must never fabricate extra days of history.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

HISTORY_DIR = Path(__file__).resolve().parent.parent / "state" / "history"

_STOPWORDS = frozenset((
    "the a an and or of to in on for with at by from as is are was were be "
    "been being this that these those it its his her their our your my i he "
    "she they we you but not has have had will would could should may might "
    "can do does did new says say said after over into out up down off than "
    "then now amid vs"
).split())

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class HistoryEntry:
    """One story's stored record for one day."""

    slug: str
    headline: str
    subject: str
    domain: str
    importance: int
    breaking: bool
    sentiment: str
    summary: str
    article_count: int
    entities: list[str] = field(default_factory=list)
    day: str = ""   # ISO date; "" until loaded from/written to a specific file

    def tokens(self) -> set[str]:
        """Significant lowercase tokens from headline+subject, plus watchlist
        entity names — the signal `find_candidates`/`group_entries` match on."""
        text = f"{self.headline} {self.subject}"
        words = {w for w in _WORD_RE.findall(text.lower())
                 if w not in _STOPWORDS and len(w) > 2}
        return words | {e.lower() for e in self.entities}

    @classmethod
    def from_story(cls, story, day: str) -> "HistoryEntry":
        """Build a record from a StorySpec-like object (duck-typed — only
        needs the attributes every StorySpec already carries, plus the
        dynamic `_articles` attribute the enrich() pipeline attaches)."""
        arts = getattr(story, "_articles", None) or []
        return cls(
            slug=story.slug, headline=story.headline, subject=story.subject,
            domain=story.domain, importance=story.importance,
            breaking=story.breaking, sentiment=story.sentiment,
            summary=story.summary, article_count=len(arts),
            entities=entities_from_articles(arts), day=day,
        )


def entities_from_articles(arts) -> list[str]:
    """Union of watchlist entity names across a list of Article-like objects
    (anything with a `.matched` attribute), de-duped, order preserved."""
    out: list[str] = []
    for a in arts:
        for name in getattr(a, "matched", None) or []:
            if name not in out:
                out.append(name)
    return out


def _day_path(day: date) -> Path:
    return HISTORY_DIR / f"{day.isoformat()}.json"


def _serialize(entries: list[HistoryEntry]) -> str:
    payload = [{k: v for k, v in asdict(e).items() if k != "day"} for e in entries]
    return json.dumps(payload, indent=2)


def write_day(day: date, entries: list[HistoryEntry], retention_days: int) -> None:
    """Persist one day's stories, then prune anything older than the
    retention window. `entries` are written without their `day` field — the
    filename is the day; `load_window` restores it on read."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _day_path(day).write_text(_serialize(entries), encoding="utf-8")
    prune(day, retention_days)


def prune(today: date, retention_days: int) -> int:
    """Delete day-files older than the retention window. Returns the count
    removed."""
    if not HISTORY_DIR.exists():
        return 0
    cutoff = today - timedelta(days=retention_days)
    removed = 0
    for f in HISTORY_DIR.glob("*.json"):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < cutoff:
            f.unlink()
            removed += 1
    return removed


def load_window(today: date, lookback_days: int) -> list[HistoryEntry]:
    """All stored entries from (today - lookback_days, today) — excludes
    today itself (there's no file for today yet when matching runs; the
    caller folds today's own in-progress stories in separately when needed,
    e.g. the weekly recap). Malformed files/filenames are skipped, not
    raised — a corrupted day-file should never take down a run."""
    if not HISTORY_DIR.exists():
        return []
    cutoff = today - timedelta(days=lookback_days)
    out: list[HistoryEntry] = []
    for f in HISTORY_DIR.glob("*.json"):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if not (cutoff < d < today):
            continue
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                try:
                    out.append(HistoryEntry(**item, day=d.isoformat()))
                except TypeError:
                    continue
    return out


@dataclass
class Candidate:
    entry: HistoryEntry
    score: float


def _overlap_score(a_tokens: set[str], b_tokens: set[str]) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = a_tokens & b_tokens
    return len(overlap) / min(len(a_tokens), len(b_tokens))


def find_candidates(headline: str, subject: str, entities: list[str],
                    window: list[HistoryEntry], threshold: float) -> list[Candidate]:
    """Deterministic token/entity overlap against the stored window — the
    cheap pre-filter that decides whether the historian pass runs at all for
    a story. No model call. A story with zero candidates costs nothing
    further downstream."""
    today = HistoryEntry(
        slug="", headline=headline, subject=subject, domain="", importance=0,
        breaking=False, sentiment="neutral", summary="", article_count=0,
        entities=entities,
    )
    today_tokens = today.tokens()
    if not today_tokens:
        return []
    out: list[Candidate] = []
    for past in window:
        score = _overlap_score(today_tokens, past.tokens())
        if score >= threshold:
            out.append(Candidate(entry=past, score=score))
    out.sort(key=lambda c: (c.entry.day, c.score), reverse=True)
    return out


def ground_truth(today_importance: int, candidates: list[Candidate]) -> dict:
    """Code-computed facts about a candidate continuation chain — the numbers
    the historian is NOT trusted to assert on its own, and what the verifier
    checks the model's prose/trend claim against. Trend compares today's
    importance to the EARLIEST candidate's (the start of the arc), not the
    most recent one."""
    if not candidates:
        return {"days_running": 1, "first_seen": None, "expected_trend": "steady"}
    days = {c.entry.day for c in candidates}
    earliest = min(candidates, key=lambda c: c.entry.day)
    delta = today_importance - earliest.entry.importance
    expected_trend = "rising" if delta > 0 else "falling" if delta < 0 else "steady"
    return {
        "days_running": len(days) + 1,     # + today
        "first_seen": min(days),
        "expected_trend": expected_trend,
    }


def trend_matches(claimed_trend: str, ground: dict) -> bool:
    """Whether the historian's claimed trend direction matches the
    code-computed ground truth — the numeric half of the verifier's honesty
    check (the other half, is_continuation, needs the adversarial model
    pass since it's a judgement call, not arithmetic)."""
    return claimed_trend == ground.get("expected_trend")
