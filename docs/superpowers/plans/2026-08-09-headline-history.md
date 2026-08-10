# Headline History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the pipeline a persistent, bounded memory of past headlines
(day/week/month buckets) and use it to add continuity notes, a curator
fatigue signal, and a weekly recap — via a `historian`/`verifier` pass pair
that mirrors the existing `extract`/`critic` grounding discipline.

**Architecture:** A new pure module (`news/history.py`) owns storage
(one JSON file per day, filtered at read time for day/week/month) and all
deterministic matching/arithmetic. Two new gemma passes in `news/agents.py`
(`historian_pass`, `verify_continuation`) supply the one thing code can't:
whether a topically-similar past story is genuinely the same ongoing event.
A new REGISTRY enricher (`news/enrichers/history.py`) renders a per-story
timeline panel; `news/enrichers/briefing.py` gains a deterministic weekly
recap panel; `news/run.py` wires the note into story markdown, the fatigue
signal into the curator's brief, and the write-after-plan/prune step.

**Tech Stack:** Python 3, existing repo deps only (stdlib `json`/`re`/
`dataclasses`/`datetime`). Tests use `pytest` (new dev dependency — this repo
has no test suite yet; model-call passes are verified manually via
`--plan-only`, matching how `extract`/`critic`/`research` are already
verified in this codebase).

## Global Constraints

- **Honesty invariant (from the spec):** day counts, first-seen dates, and
  the rising/falling/steady trend direction are always code-computed from
  the stored record — never asserted by a model. The historian pass only
  supplies the semantic "is this really the same story" judgement and a
  one-sentence note; the verifier both re-judges that adversarially AND the
  note's claims are checked against the code-computed ground truth.
- **Fail-soft everywhere:** any rejection (no candidates, historian says no,
  trend mismatch, verifier rejects) drops `_continuity` entirely — the story
  renders exactly as it does today, no exceptions raised.
- **`state/history/` is written only on real runs**, never `--plan-only` —
  same rule the article body cache does *not* need (that cache is fine to
  grow under `--plan-only`) but history explicitly does, since a dev loop
  re-running the same morning must not fabricate multiple "days" of history.
- **Retention:** `history.retention_days` config default is **60** (per your
  last decision — covers the month bucket with headroom).
- Follow existing module conventions: `from __future__ import annotations`
  at the top of every new file; dataclasses for structured records; pure
  functions import `SourceData`/`T_STR`/`T_INT` lazily inside `build()` the
  same way every other enricher does (avoids the circular-import pattern
  noted in `enrichers/__init__.py`).

---

## Task 1: Test infra + history store (write/prune/load)

**Files:**
- Create: `news/history.py`
- Create: `tests/test_history.py`
- Create: `pytest.ini`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `news.history.HISTORY_DIR: Path`, `news.history.HistoryEntry`
  (dataclass: `slug, headline, subject, domain, importance, breaking,
  sentiment, summary, article_count, entities: list[str], day: str = ""`),
  `news.history.write_day(day: date, entries: list[HistoryEntry],
  retention_days: int) -> None`, `news.history.prune(today: date,
  retention_days: int) -> int`, `news.history.load_window(today: date,
  lookback_days: int) -> list[HistoryEntry]`.

- [ ] **Step 1: Add pytest and create the pytest config**

Append to `requirements.txt`:

```
# Dev/testing only — not required to run the daily pipeline.
pytest>=8.0.0
```

Create `pytest.ini`:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 2: Install pytest**

Run: `./.venv/bin/pip install -r requirements.txt`
Expected: pytest installs cleanly alongside the existing deps.

- [ ] **Step 3: Write the failing tests for the store layer**

Create `tests/test_history.py`:

```python
from datetime import date

import pytest

from news import history


@pytest.fixture(autouse=True)
def isolated_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    return tmp_path / "history"


def _entry(slug="fed-rate-cut", headline="Fed cuts rates a quarter point",
           subject="Federal Reserve rate policy", domain="markets",
           importance=4, day=""):
    return history.HistoryEntry(
        slug=slug, headline=headline, subject=subject, domain=domain,
        importance=importance, breaking=False, sentiment="neutral",
        summary="The Fed cut rates.", article_count=5,
        entities=["Federal Reserve"], day=day,
    )


def test_write_day_creates_one_file_per_day(isolated_history_dir):
    history.write_day(date(2026, 8, 9), [_entry()], retention_days=60)
    f = isolated_history_dir / "2026-08-09.json"
    assert f.exists()
    payload = f.read_text(encoding="utf-8")
    assert "fed-rate-cut" in payload
    assert '"day"' not in payload   # day is implied by the filename


def test_write_day_prunes_files_older_than_retention(isolated_history_dir):
    isolated_history_dir.mkdir(parents=True)
    (isolated_history_dir / "2026-01-01.json").write_text("[]", encoding="utf-8")
    history.write_day(date(2026, 8, 9), [_entry()], retention_days=60)
    assert not (isolated_history_dir / "2026-01-01.json").exists()
    assert (isolated_history_dir / "2026-08-09.json").exists()


def test_load_window_excludes_today_and_out_of_range_days(isolated_history_dir):
    history.write_day(date(2026, 8, 2), [_entry(slug="week-old")], retention_days=60)
    history.write_day(date(2026, 8, 8), [_entry(slug="yesterday")], retention_days=60)
    # A file for "today" (2026-08-09) should never be read by load_window,
    # since it represents the run currently being built.
    isolated_history_dir_path = isolated_history_dir
    (isolated_history_dir_path / "2026-08-09.json").write_text(
        history._serialize([_entry(slug="today")]), encoding="utf-8")

    window = history.load_window(date(2026, 8, 9), lookback_days=7)
    slugs = {e.slug for e in window}
    assert slugs == {"yesterday"}   # 2026-08-02 is 7 days back — out of a 7-day window; today excluded


def test_load_window_skips_malformed_files(isolated_history_dir):
    isolated_history_dir.mkdir(parents=True)
    (isolated_history_dir / "2026-08-08.json").write_text("not json", encoding="utf-8")
    (isolated_history_dir / "not-a-date.json").write_text("[]", encoding="utf-8")
    window = history.load_window(date(2026, 8, 9), lookback_days=7)
    assert window == []


def test_load_window_returns_no_files_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "does-not-exist")
    assert history.load_window(date(2026, 8, 9), lookback_days=7) == []
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'news.history'`

- [ ] **Step 5: Implement the store layer**

Create `news/history.py`:

```python
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
    """All stored entries from [today - lookback_days, today) — excludes
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
        if not (cutoff <= d < today):
            continue
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_history.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add news/history.py tests/test_history.py pytest.ini requirements.txt
git commit -m "history: persistent day-file store (write/prune/load window)"
```

---

## Task 2: Candidate matching + entity extraction

**Files:**
- Modify: `news/history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `HistoryEntry.tokens()`, `HistoryEntry` (Task 1).
- Produces: `news.history.Candidate` (dataclass: `entry: HistoryEntry, score:
  float`), `news.history.find_candidates(headline: str, subject: str,
  entities: list[str], window: list[HistoryEntry], threshold: float) ->
  list[Candidate]` — sorted most-recent-day first, `entry.day` descending.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
def test_find_candidates_matches_on_shared_headline_tokens():
    window = [
        history.HistoryEntry(
            slug="fed-considers-cut", headline="Fed considers a quarter-point rate cut",
            subject="Federal Reserve", domain="markets", importance=3,
            breaking=False, sentiment="neutral", summary="", article_count=3,
            entities=["Federal Reserve"], day="2026-08-06",
        ),
        history.HistoryEntry(
            slug="padres-win", headline="Padres win 4-2 over the Giants",
            subject="Padres", domain="sports", importance=2, breaking=False,
            sentiment="good", summary="", article_count=2, entities=[],
            day="2026-08-06",
        ),
    ]
    candidates = history.find_candidates(
        "Fed cuts rates a quarter point", "Federal Reserve rate policy",
        ["Federal Reserve"], window, threshold=0.3)
    assert [c.entry.slug for c in candidates] == ["fed-considers-cut"]
    assert candidates[0].score > 0


def test_find_candidates_respects_threshold():
    window = [history.HistoryEntry(
        slug="loose-overlap", headline="A story about the general economy",
        subject="", domain="business", importance=3, breaking=False,
        sentiment="neutral", summary="", article_count=1, entities=[],
        day="2026-08-06",
    )]
    candidates = history.find_candidates(
        "Fed cuts rates a quarter point", "", [], window, threshold=0.9)
    assert candidates == []


def test_find_candidates_empty_window_or_blank_story_returns_empty():
    assert history.find_candidates("Fed cuts rates", "", [], [], threshold=0.3) == []
    entry = history.HistoryEntry(
        slug="x", headline="Fed cuts rates", subject="", domain="markets",
        importance=3, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day="2026-08-06")
    assert history.find_candidates("", "", [], [entry], threshold=0.3) == []


def test_find_candidates_sorted_most_recent_day_first():
    older = history.HistoryEntry(
        slug="fed-a", headline="Fed weighs a rate cut", subject="", domain="markets",
        importance=3, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day="2026-08-05")
    newer = history.HistoryEntry(
        slug="fed-b", headline="Fed signals a rate cut", subject="", domain="markets",
        importance=3, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day="2026-08-07")
    candidates = history.find_candidates(
        "Fed cuts rates", "", [], [older, newer], threshold=0.2)
    assert [c.entry.slug for c in candidates] == ["fed-b", "fed-a"]


def test_entities_from_articles_dedupes_preserving_order():
    class FakeArticle:
        def __init__(self, matched):
            self.matched = matched

    arts = [FakeArticle(["Apple", "Nvidia"]), FakeArticle(["Nvidia", "Amazon"])]
    assert history.entities_from_articles(arts) == ["Apple", "Nvidia", "Amazon"]


def test_entities_from_articles_handles_no_matched_attr():
    class Bare:
        pass

    assert history.entities_from_articles([Bare()]) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_history.py -v -k find_candidates`
Expected: FAIL — `AttributeError: module 'news.history' has no attribute 'find_candidates'`

- [ ] **Step 3: Implement matching**

Append to `news/history.py` (after `load_window`):

```python
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
```

- [ ] **Step 4: Run to verify all history tests pass**

Run: `./.venv/bin/pytest tests/test_history.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add news/history.py tests/test_history.py
git commit -m "history: candidate matching + entity extraction"
```

---

## Task 3: Ground-truth arithmetic (day count, trend)

**Files:**
- Modify: `news/history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `Candidate` (Task 2).
- Produces: `news.history.ground_truth(today_importance: int,
  candidates: list[Candidate]) -> dict` (keys: `days_running: int,
  first_seen: str | None, expected_trend: str`), `news.history.trend_matches(
  claimed_trend: str, ground: dict) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
def _candidate(day, importance):
    entry = history.HistoryEntry(
        slug="x", headline="Fed cuts rates", subject="", domain="markets",
        importance=importance, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day=day)
    return history.Candidate(entry=entry, score=0.5)


def test_ground_truth_no_candidates():
    g = history.ground_truth(4, [])
    assert g == {"days_running": 1, "first_seen": None, "expected_trend": "steady"}


def test_ground_truth_days_running_counts_distinct_days_plus_today():
    candidates = [_candidate("2026-08-06", 2), _candidate("2026-08-07", 3),
                  _candidate("2026-08-08", 3)]
    g = history.ground_truth(4, candidates)
    assert g["days_running"] == 4          # 3 distinct past days + today
    assert g["first_seen"] == "2026-08-06"


def test_ground_truth_expected_trend_rising():
    g = history.ground_truth(5, [_candidate("2026-08-06", 2)])
    assert g["expected_trend"] == "rising"


def test_ground_truth_expected_trend_falling():
    g = history.ground_truth(2, [_candidate("2026-08-06", 5)])
    assert g["expected_trend"] == "falling"


def test_ground_truth_expected_trend_steady():
    g = history.ground_truth(3, [_candidate("2026-08-06", 3)])
    assert g["expected_trend"] == "steady"


def test_ground_truth_uses_earliest_candidate_for_delta():
    # Earliest (by day) candidate's importance is the baseline, not the latest.
    candidates = [_candidate("2026-08-06", 1), _candidate("2026-08-08", 5)]
    g = history.ground_truth(5, candidates)
    assert g["expected_trend"] == "rising"   # 5 - 1(earliest) > 0


def test_trend_matches():
    ground = {"days_running": 2, "first_seen": "2026-08-08", "expected_trend": "rising"}
    assert history.trend_matches("rising", ground) is True
    assert history.trend_matches("falling", ground) is False
    assert history.trend_matches("steady", ground) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_history.py -v -k ground_truth`
Expected: FAIL — `AttributeError: module 'news.history' has no attribute 'ground_truth'`

- [ ] **Step 3: Implement**

Append to `news/history.py`:

```python
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
```

- [ ] **Step 4: Run to verify all pass**

Run: `./.venv/bin/pytest tests/test_history.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add news/history.py tests/test_history.py
git commit -m "history: ground-truth day-count/trend arithmetic"
```

---

## Task 4: Continuation-chain clustering (for the weekly recap)

**Files:**
- Modify: `news/history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `HistoryEntry.tokens()`, `_overlap_score` (Task 2).
- Produces: `news.history.group_entries(entries: list[HistoryEntry],
  threshold: float) -> list[list[HistoryEntry]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
def test_group_entries_clusters_a_multi_day_arc():
    entries = [
        history.HistoryEntry(slug="a", headline="Fed weighs a rate cut", subject="",
                             domain="markets", importance=2, breaking=False,
                             sentiment="neutral", summary="", article_count=1,
                             entities=["Federal Reserve"], day="2026-08-05"),
        history.HistoryEntry(slug="b", headline="Fed signals a rate cut", subject="",
                             domain="markets", importance=3, breaking=False,
                             sentiment="neutral", summary="", article_count=1,
                             entities=["Federal Reserve"], day="2026-08-07"),
        history.HistoryEntry(slug="c", headline="Fed cuts rates a quarter point",
                             subject="", domain="markets", importance=4,
                             breaking=False, sentiment="neutral", summary="",
                             article_count=1, entities=["Federal Reserve"],
                             day="2026-08-09"),
    ]
    groups = history.group_entries(entries, threshold=0.3)
    assert len(groups) == 1
    assert {e.slug for e in groups[0]} == {"a", "b", "c"}


def test_group_entries_keeps_unrelated_stories_separate():
    entries = [
        history.HistoryEntry(slug="fed", headline="Fed cuts rates", subject="",
                             domain="markets", importance=3, breaking=False,
                             sentiment="neutral", summary="", article_count=1,
                             entities=[], day="2026-08-06"),
        history.HistoryEntry(slug="padres", headline="Padres win the series", subject="",
                             domain="sports", importance=3, breaking=False,
                             sentiment="good", summary="", article_count=1,
                             entities=[], day="2026-08-07"),
    ]
    groups = history.group_entries(entries, threshold=0.3)
    assert len(groups) == 2


def test_group_entries_empty_list():
    assert history.group_entries([], threshold=0.3) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_history.py -v -k group_entries`
Expected: FAIL — `AttributeError`

- [ ] **Step 3: Implement**

Append to `news/history.py`:

```python
def group_entries(entries: list[HistoryEntry], threshold: float) -> list[list[HistoryEntry]]:
    """Greedy connected-components clustering by token/entity overlap —
    collapses a multi-day continuation chain into one group so a recap
    counts it once, at its peak importance. Pairwise, O(n^2), fine at the
    scale this runs at (a handful of stories/day over a ~7-day window)."""
    n = len(entries)
    if n == 0:
        return []
    token_sets = [e.tokens() for e in entries]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _overlap_score(token_sets[i], token_sets[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[HistoryEntry]] = {}
    for i, e in enumerate(entries):
        groups.setdefault(find(i), []).append(e)
    return list(groups.values())
```

- [ ] **Step 4: Run to verify all pass**

Run: `./.venv/bin/pytest tests/test_history.py -v`
Expected: all passed (18 total)

- [ ] **Step 5: Commit**

```bash
git add news/history.py tests/test_history.py
git commit -m "history: continuation-chain clustering for the weekly recap"
```

---

## Task 5: Config — `history:` block + historian/verifier model config

**Files:**
- Modify: `config/outlets.yaml`

**Interfaces:**
- Produces: config keys `history.retention_days`, `history.match_threshold`,
  `history.recap.lookback_days`, `history.recap.max_stories`,
  `models.historian`, `models.verifier`, `reasoning.historian`,
  `reasoning.verifier` — consumed by Task 6/7/10.

- [ ] **Step 1: Add the two new models to `models:`**

In `config/outlets.yaml`, find the `models:` block (starts `models:` before
`# gpt-oss reasoning effort per pass`) and add two lines after `critic`:

```yaml
  critic: "gpt-oss:latest"      # audits each extracted figure against the source text
  historian: "gpt-oss:latest"   # judges whether a matched past story is really the same one
  verifier: "gpt-oss:latest"    # adversarially audits the historian's continuation claim
```

- [ ] **Step 2: Add reasoning effort for both**

In the `reasoning:` block, add after `critic`:

```yaml
  critic: high               # also the relevance judge (reject off-topic figures a
                              # same-vocabulary article leaked in) — wants real reasoning
  historian: medium           # narrower judgement than triage — is THIS candidate really
                               # the same ongoing story, not just the same keywords
  verifier: high               # adversarial audit of the historian's claim, same
                                # skeptical-by-default posture as the numbers critic
```

- [ ] **Step 3: Add the `history:` config block**

Insert a new `history:` block after the `research:` block (before `helio:`):

```yaml
# Headline history (day/week/month memory). A `historian` pass judges whether
# a candidate past story (found by deterministic keyword/entity overlap, no
# model call) is genuinely the SAME ongoing story as today's; a `verifier`
# pass adversarially audits that judgement, same relevance-judge role the
# numbers critic plays for facts. Day counts/trend direction are always
# code-computed from the stored record, never asserted by a model — see
# docs/superpowers/specs/2026-08-09-headline-history-design.md.
history:
  retention_days: 60          # how long a day's stored headlines stick around
  match_threshold: 0.35       # token/entity overlap score to become a "candidate"
  recap:
    lookback_days: 7          # the weekly recap panel's window
    max_stories: 6            # top-N continuation-chains shown, by peak importance
```

- [ ] **Step 4: Verify the config still parses**

Run: `./.venv/bin/python -c "from news.fetch import load_config; c = load_config(); print(c['history']); print(c['models']['historian']); print(c['reasoning']['verifier'])"`
Expected: prints the `history` dict, `gpt-oss:latest`, `high` — no traceback.

- [ ] **Step 5: Commit**

```bash
git add config/outlets.yaml
git commit -m "config: add history block + historian/verifier model config"
```

---

## Task 6: Historian + verifier gemma passes

**Files:**
- Modify: `news/agents.py`
- Create: `tests/test_agents_continuity.py`

**Interfaces:**
- Consumes: `news.history.{Candidate, ground_truth, trend_matches,
  find_candidates}` (Tasks 2/3), `Ollama.chat_json(model, system, user,
  temperature=0.2, think=None) -> dict` (existing, `news/agents.py`).
- Produces: `news.agents.historian_pass(ollama, model: str, story: dict,
  candidates: list[dict], think: str | None = None) -> dict` (keys
  `is_continuation: bool, trend: str, note: str`),
  `news.agents.verify_continuation(ollama, model: str, story: dict,
  candidates: list[dict], historian_out: dict, think: str | None = None) ->
  bool`, `news.agents.continuity_facts(ollama, model_historian: str,
  model_verifier: str, story: dict, entities: list[str],
  window: list, match_threshold: float, think_historian: str | None = None,
  think_verifier: str | None = None) -> dict | None` — the returned dict's
  shape (consumed by Tasks 7/8/9/10):
  `{"is_continuation": True, "days_running": int, "first_seen": str,
  "trend": str, "note": str, "occurrences": [{"day","headline","importance"}]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agents_continuity.py`:

```python
from news import agents, history


class FakeOllama:
    """Stub matching Ollama.chat_json's signature — no network/ollama needed."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, model, system, user, temperature=0.2, think=None):
        self.calls.append({"model": model, "system": system, "user": user,
                           "temperature": temperature, "think": think})
        return self.responses.pop(0) if self.responses else {}


def _candidate_dicts():
    return [{"day": "2026-08-08", "headline": "Fed signals a rate cut", "importance": 3}]


def _story():
    return {"headline": "Fed cuts rates a quarter point", "domain": "markets",
            "importance": 4, "breaking": False, "slug": "fed-rate-cut"}


def _window(day="2026-08-08", importance=3):
    entry = history.HistoryEntry(
        slug="fed-signals", headline="Fed signals a rate cut", subject="",
        domain="markets", importance=importance, breaking=False,
        sentiment="neutral", summary="", article_count=2,
        entities=["Federal Reserve"], day=day)
    return [entry]


# ── historian_pass ───────────────────────────────────────────────────────────

def test_historian_pass_short_circuits_with_no_candidates():
    ollama = FakeOllama([])
    out = agents.historian_pass(ollama, "gpt-oss:latest", _story(), [])
    assert out == {"is_continuation": False, "trend": "steady", "note": ""}
    assert ollama.calls == []


def test_historian_pass_parses_a_valid_response():
    ollama = FakeOllama([{"is_continuation": True, "trend": "rising",
                          "note": "Second day of coverage."}])
    out = agents.historian_pass(ollama, "gpt-oss:latest", _story(), _candidate_dicts())
    assert out == {"is_continuation": True, "trend": "rising",
                   "note": "Second day of coverage."}


def test_historian_pass_invalid_trend_defaults_to_steady():
    ollama = FakeOllama([{"is_continuation": True, "trend": "sideways", "note": "x"}])
    out = agents.historian_pass(ollama, "gpt-oss:latest", _story(), _candidate_dicts())
    assert out["trend"] == "steady"


# ── verify_continuation ──────────────────────────────────────────────────────

def test_verify_continuation_true_when_model_confirms():
    ollama = FakeOllama([{"confirmed": True}])
    result = agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": True, "trend": "rising", "note": "x"})
    assert result is True


def test_verify_continuation_false_when_model_rejects():
    ollama = FakeOllama([{"confirmed": False}])
    result = agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": True, "trend": "rising", "note": "x"})
    assert result is False


def test_verify_continuation_short_circuits_when_historian_said_no():
    ollama = FakeOllama([])
    result = agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": False, "trend": "steady", "note": ""})
    assert result is False
    assert ollama.calls == []


# ── continuity_facts orchestrator ────────────────────────────────────────────

def test_continuity_facts_none_when_no_matching_candidates():
    ollama = FakeOllama([])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(), [], [],
        match_threshold=0.35)
    assert result is None
    assert ollama.calls == []


def test_continuity_facts_none_when_historian_says_not_a_continuation():
    ollama = FakeOllama([{"is_continuation": False, "trend": "steady", "note": ""}])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(), match_threshold=0.2)
    assert result is None


def test_continuity_facts_none_when_trend_claim_mismatches_ground_truth():
    # today's importance (4) vs earliest candidate (3) → expected_trend "rising";
    # historian claims "falling" — should be rejected before the verifier ever runs.
    ollama = FakeOllama([{"is_continuation": True, "trend": "falling", "note": "x"}])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(importance=3), match_threshold=0.2)
    assert result is None
    assert len(ollama.calls) == 1   # only the historian call — verifier never ran


def test_continuity_facts_none_when_verifier_rejects():
    ollama = FakeOllama([
        {"is_continuation": True, "trend": "rising", "note": "Second day."},
        {"confirmed": False},
    ])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(importance=3), match_threshold=0.2)
    assert result is None


def test_continuity_facts_confirmed_returns_full_dict():
    ollama = FakeOllama([
        {"is_continuation": True, "trend": "rising", "note": "Second day of coverage."},
        {"confirmed": True},
    ])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(importance=3), match_threshold=0.2)
    assert result == {
        "is_continuation": True,
        "days_running": 2,
        "first_seen": "2026-08-08",
        "trend": "rising",
        "note": "Second day of coverage.",
        "occurrences": [{"day": "2026-08-08", "headline": "Fed signals a rate cut",
                         "importance": 3}],
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_agents_continuity.py -v`
Expected: FAIL — `AttributeError: module 'news.agents' has no attribute 'historian_pass'`

- [ ] **Step 3: Implement the two passes + orchestrator**

In `news/agents.py`, add after `numeric_facts` (end of the "by-the-numbers"
section, before the `_SENTIMENT_SYS` block):

```python
# ── pass 3e: headline continuity (historian + adversarial verifier) ──────────
# Two turns, both gpt-oss, mirroring extract→critic: the HISTORIAN judges the
# one thing code can't — is a keyword-matched past story really the SAME
# ongoing event, or coincidental topic overlap? — and drafts a one-sentence
# continuity note. Day counts and trend direction are never trusted from the
# model: news.history.ground_truth computes them from the stored record, and
# a mismatch between the historian's trend claim and that ground truth is
# rejected before the verifier is even spent. The VERIFIER then adversarially
# re-judges is_continuation, same skeptical-by-default posture as the numbers
# critic. Either check failing drops continuity for that story entirely — it
# just renders with no continuity note/panel, same fail-soft rule as every
# other enricher.

_HISTORIAN_SYS = (
    "You are a news editor deciding whether TODAY's story is a continuation of "
    "stories your desk already covered on past days, or just a coincidental topic "
    "overlap. You are given today's story and a list of CANDIDATE past stories "
    "that share keywords with it (each with its date, headline, and importance "
    "1-5). Decide is_continuation=true ONLY if the candidates are genuinely "
    "reporting on the SAME ongoing event or situation as today's story — not "
    "merely the same topic area (e.g. two different Fed stories a week apart "
    "about DIFFERENT policy actions are NOT a continuation; a team's game "
    "yesterday and a DIFFERENT game today are NOT a continuation). If true, "
    "judge whether the story's importance has been rising, falling, or holding "
    "steady across the days, and write ONE sentence framing the continuity for "
    "a reader (e.g. 'Fourth consecutive day of coverage, escalating from a "
    "routine update to today's lead story.'). If is_continuation=false, set "
    "trend='steady' and note=''. Return ONLY JSON "
    '{"is_continuation","trend","note"}.'
)


def historian_pass(ollama: Ollama, model: str, story: dict, candidates: list[dict],
                   think: str | None = None) -> dict:
    """Judges whether `candidates` (real stored past occurrences, already
    matched by news.history.find_candidates — never invented here) are
    genuinely the same ongoing story as `story`, and drafts a continuity
    note. Short-circuits with no ollama call when there are no candidates."""
    if not candidates:
        return {"is_continuation": False, "trend": "steady", "note": ""}
    listed = "\n".join(
        f'- {c["day"]}: "{c["headline"]}" (importance {c["importance"]}/5)'
        for c in candidates
    )
    user = (
        f"Today's story: {story.get('headline')}\n"
        f"Domain: {story.get('domain', '')}\n"
        f"Importance: {story.get('importance')}/5\n\n"
        f"Candidate past stories (share keywords with today's):\n{listed}"
    )
    out = ollama.chat_json(model, _HISTORIAN_SYS, user, temperature=0.2, think=think)
    trend = str(out.get("trend", "")).strip().lower()
    return {
        "is_continuation": bool(out.get("is_continuation")),
        "trend": trend if trend in ("rising", "falling", "steady") else "steady",
        "note": str(out.get("note") or "").strip()[:240],
    }


_VERIFY_CONTINUATION_SYS = (
    "You are a skeptical editor double-checking a colleague's claim that TODAY's "
    "story is a continuation of specific past stories. You are given today's "
    "story, the past stories claimed as the same ongoing event, and the "
    "colleague's one-sentence note. Reject (confirmed=false) UNLESS the past "
    "stories are clearly about the SAME event or situation as today's, not just "
    "the same general topic — when genuinely uncertain, reject. Also reject if "
    "the note asserts anything not supported by the listed stories. Return ONLY "
    'JSON {"confirmed"}.'
)


def verify_continuation(ollama: Ollama, model: str, story: dict, candidates: list[dict],
                        historian_out: dict, think: str | None = None) -> bool:
    """Adversarial audit of the historian's is_continuation claim — same
    relevance-judge role critic_numbers plays for extracted figures. Never
    called (no ollama spend) when the historian already said no."""
    if not candidates or not historian_out.get("is_continuation"):
        return False
    listed = "\n".join(
        f'- {c["day"]}: "{c["headline"]}" (importance {c["importance"]}/5)'
        for c in candidates
    )
    user = (
        f"Today's story: {story.get('headline')}\n\n"
        f"Claimed past occurrences:\n{listed}\n\n"
        f"Colleague's note: \"{historian_out.get('note', '')}\""
    )
    out = ollama.chat_json(model, _VERIFY_CONTINUATION_SYS, user,
                           temperature=0.1, think=think)
    return bool(out.get("confirmed"))


def continuity_facts(ollama: Ollama, model_historian: str, model_verifier: str,
                     story: dict, entities: list[str], window: list,
                     match_threshold: float, think_historian: str | None = None,
                     think_verifier: str | None = None) -> dict | None:
    """Full continuity pipeline for one story → a `_continuity` dict, or
    None. Code matches candidates (news.history.find_candidates) and computes
    the ground-truth day-count/trend; the historian judges is_continuation +
    drafts the note; a code-side trend check runs before spending the
    verifier call; the verifier then adversarially audits is_continuation.
    Any failure at any step → None, so the story just renders with no
    continuity data — same fail-soft posture every enricher already has."""
    from . import history as _history

    candidates = _history.find_candidates(
        story.get("headline", ""), story.get("subject", ""), entities,
        window, match_threshold)
    if not candidates:
        return None

    candidate_dicts = [{"day": c.entry.day, "headline": c.entry.headline,
                        "importance": c.entry.importance} for c in candidates]

    with timed("historian"):
        h_out = historian_pass(ollama, model_historian, story, candidate_dicts,
                               think_historian)
    if not h_out["is_continuation"]:
        return None

    ground = _history.ground_truth(int(story.get("importance") or 0), candidates)
    if not _history.trend_matches(h_out["trend"], ground):
        return None            # historian's own trend claim already inconsistent

    with timed("verifier"):
        confirmed = verify_continuation(ollama, model_verifier, story,
                                        candidate_dicts, h_out, think_verifier)
    if not confirmed:
        return None

    return {
        "is_continuation": True,
        "days_running": ground["days_running"],
        "first_seen": ground["first_seen"],
        "trend": h_out["trend"],
        "note": h_out["note"],
        "occurrences": candidate_dicts,
    }
```

- [ ] **Step 4: Run to verify all pass**

Run: `./.venv/bin/pytest tests/test_agents_continuity.py -v`
Expected: 11 passed

- [ ] **Step 5: Run the full test suite to check nothing else broke**

Run: `./.venv/bin/pytest -v`
Expected: all passed (Tasks 1-6's tests)

- [ ] **Step 6: Commit**

```bash
git add news/agents.py tests/test_agents_continuity.py
git commit -m "agents: historian + verifier continuity passes"
```

---

## Task 7: Wire continuity into `enrich()` + the planner's menu

**Files:**
- Modify: `news/agents.py`
- Modify: `tests/test_agents_continuity.py`

**Interfaces:**
- Consumes: `continuity_facts` (Task 6), `history.load_window` (Task 1),
  `history.entities_from_articles` (Task 2).
- Produces: `story_offers(..., history_occurrences: int = 0)` gains the
  `history:timeline` menu entry; `enrich()` attaches `story._continuity`
  (dict from Task 6, or `None`) to every `StorySpec` it returns, and loads
  the history window once per run (not once per story).

- [ ] **Step 1: Write the failing test for the offers menu**

Append to `tests/test_agents_continuity.py`:

```python
def test_story_offers_includes_history_timeline_at_three_occurrences():
    offers = agents.story_offers({}, [], {}, has_image=False, n_facts=0,
                                 history_occurrences=3)
    keys = {k for k, _ in offers}
    assert "history:timeline" in keys


def test_story_offers_omits_history_timeline_below_three():
    offers = agents.story_offers({}, [], {}, has_image=False, n_facts=0,
                                 history_occurrences=2)
    keys = {k for k, _ in offers}
    assert "history:timeline" not in keys


def test_story_offers_defaults_history_occurrences_to_zero():
    offers = agents.story_offers({}, [], {}, has_image=False, n_facts=0)
    keys = {k for k, _ in offers}
    assert "history:timeline" not in keys
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_agents_continuity.py -v -k history_timeline`
Expected: FAIL — `TypeError: story_offers() got an unexpected keyword argument 'history_occurrences'`

- [ ] **Step 3: Add the offer**

First, add the import alongside the existing `_coverage` import near the top
of `news/agents.py` (find `from .enrichers import coverage as _coverage`):

```python
from .enrichers import coverage as _coverage
from .enrichers import history as _history_enricher
```

Then update `story_offers()` — add the parameter and the offer, right after
the `facts:numbers` block. Reference `_history_enricher.MIN_OCCURRENCES`
(Task 8 defines it) rather than hardcoding the threshold a second time —
`enrichers/history.py`'s `build()` is the single source of truth for how
many occurrences actually render the panel. (This is already how the
implemented code works — verified by Task 7's review — this section
documents it for the record after a stray uncommitted-edit loss during
execution; see the ledger.)

```python
def story_offers(story: dict, arts: list[Article], story_tickers: dict[str, str],
                 has_image: bool, n_facts: int = 0,
                 series_specs: list[dict] | None = None,
                 research_label: str = "",
                 history_occurrences: int = 0) -> list[tuple[str, str]]:
    """The real menu for one story: (data key, human description) for every panel
    whose data we can actually produce right now. Computed in code — never by the
    model — so the planner can only pick things that will really render."""
    offers: list[tuple[str, str]] = []

    if has_image:
        src = next((a.source for a in arts if getattr(a, "image_url", "")), "a wire")
        offers.append(("image", f"type=image — the story's news photo (from {src})"))

    if n_facts >= MIN_NUMERIC_FACTS:
        offers.append(("facts:numbers",
                       f"type=table — the {n_facts} key figures in this story "
                       f"(amounts, counts, %) pulled and fact-checked from the "
                       f"reporting, shown as a grid of stat tiles"))

    if history_occurrences >= _history_enricher.MIN_OCCURRENCES:
        offers.append(("history:timeline",
                       f"type=table — this story's last {history_occurrences} days "
                       f"of coverage, a quick timeline of how it developed"))

    for mode in _coverage.available(arts):
```

(The rest of the function is unchanged — this only adds the one new `if`
block between the existing `facts:numbers` block and the `coverage` loop.)

- [ ] **Step 4: Run to verify the offers tests pass**

Run: `./.venv/bin/pytest tests/test_agents_continuity.py -v`
Expected: 14 passed

- [ ] **Step 5: Wire `continuity_facts` into `enrich()`**

**Pipeline-ordering refinement vs. the design doc:** the spec describes
historian/verifier as running "after summarizer" — true at the conceptual
level (it's one of the last per-story judgement passes), but the *offer*
needs `len(continuity["occurrences"])` before `story_offers()`/`plan_story()`
run, and those already run *before* `summarize_story()` in today's `enrich()`
loop (exactly like `numeric_facts`/`_central_tickers`/`_central_series`,
which also fully resolve before the planner sees its menu). So continuity is
computed on the **raw triage headline** (`st["headline"]`), not the
summarizer's polished one — same timing as every other offer-gating signal.
The historian's note is a short, independent blurb anyway (not a rewrite of
the summary), so the raw vs. polished headline distinction doesn't affect
its quality, and this keeps the "offers reflect only fully-resolved data"
invariant the rest of `enrich()` already holds.

In `news/agents.py`, `enrich()`: load the history window once before the
per-story loop, then compute continuity per story and attach it. Three
small edits:

First, near the top of `enrich()` (right after `plan = DayPlan(...)`), load
the window once:

```python
    plan = DayPlan(day=run_day or date.today())
    research_spent = 0                        # per-run budget for the research agent
    hist_cfg = config.get("history", {})
    from . import history as _history
    history_window = _history.load_window(plan.day, int(hist_cfg.get("retention_days", 60)))
```

Then, inside the per-story loop, right after the `series_specs = (...)` line
and before `research_series = None`, compute continuity:

```python
        entities = _history.entities_from_articles(arts)
        continuity = continuity_facts(
            ollama, models.get("historian", "gpt-oss:latest"),
            models.get("verifier", "gpt-oss:latest"), st, entities, history_window,
            float(hist_cfg.get("match_threshold", 0.35)),
            effort.get("historian"), effort.get("verifier"),
        )
```

Then update the `offers = story_offers(...)` call to pass it through:

```python
        offers = story_offers(st, arts, tickers, has_image, len(facts), series_specs,
                              research_label=(research_series.label if research_series else ""),
                              history_occurrences=len(continuity["occurrences"]) if continuity else 0)
```

And finally, where `spec._research = research_series` is set, add:

```python
            spec._research = research_series  # type: ignore[attr-defined]
            spec._continuity = continuity      # type: ignore[attr-defined]
```

- [ ] **Step 6: Sanity-check the wiring compiles and imports cleanly**

Run: `./.venv/bin/python -c "from news import agents; print('ok')"`
Expected: `ok` — no `ImportError`/`SyntaxError`.

Note: `enrich()` itself is not unit-tested end-to-end here — it requires a
live ollama server for `triage`/`planner`/`summarizer`, same as today. This
matches the existing convention in this codebase (see `numeric_facts`/
`research_series`, neither of which has an `enrich()`-level test either).
The full wiring is verified manually in Task 11 via `--plan-only`.

- [ ] **Step 7: Run the full test suite**

Run: `./.venv/bin/pytest -v`
Expected: all passed

- [ ] **Step 8: Commit**

```bash
git add news/agents.py tests/test_agents_continuity.py
git commit -m "agents: wire continuity into enrich() and the planner menu"
```

---

## Task 8: `history:timeline` enricher + registry wiring

**Files:**
- Create: `news/enrichers/history.py`
- Modify: `news/enrichers/__init__.py`
- Modify: `news/plan_schema.py`
- Create: `tests/test_enrichers_history.py`

**Interfaces:**
- Consumes: `story._continuity` (dynamic attribute set by Task 7's
  `enrich()`), `SourceData`/`T_STR`/`T_INT` (`news/enrichers/__init__.py`).
- Produces: `news.enrichers.history.build(arg, panel, story) -> SourceData |
  None`, registered as `REGISTRY["history"]` and in
  `plan_schema.KNOWN_ENRICHERS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_enrichers_history.py`:

```python
from types import SimpleNamespace

from news.enrichers import history as history_enricher


def _story(continuity=None, slug="fed-rate-cut"):
    return SimpleNamespace(slug=slug, _continuity=continuity)


def test_build_returns_none_without_continuity():
    assert history_enricher.build("timeline", None, _story(continuity=None)) is None


def test_build_returns_none_below_min_occurrences():
    continuity = {"occurrences": [{"day": "2026-08-08", "headline": "x", "importance": 3},
                                  {"day": "2026-08-07", "headline": "y", "importance": 2}]}
    assert history_enricher.build("timeline", None, _story(continuity=continuity)) is None


def test_build_returns_table_when_confirmed():
    continuity = {"occurrences": [
        {"day": "2026-08-06", "headline": "Fed weighs a rate cut", "importance": 2},
        {"day": "2026-08-07", "headline": "Fed signals a rate cut", "importance": 3},
        {"day": "2026-08-08", "headline": "Fed set to cut rates", "importance": 3},
    ]}
    sd = history_enricher.build("timeline", None, _story(continuity=continuity))
    assert sd is not None
    assert sd.panel_type == "table"
    assert sd.key == "history-fed-rate-cut-timeline"
    assert len(sd.rows) == 3
    assert sd.rows[0] == ["2026-08-06", "Fed weighs a rate cut", 2]
    assert sd.column_order == ["date", "headline", "importance"]


def test_registered_in_registry_and_known_enrichers():
    from news import enrichers, plan_schema

    assert "history" in enrichers.REGISTRY
    assert enrichers.REGISTRY["history"] is history_enricher.build
    assert "history" in plan_schema.KNOWN_ENRICHERS
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_enrichers_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'news.enrichers.history'`

- [ ] **Step 3: Implement the enricher**

Create `news/enrichers/history.py`:

```python
"""history:timeline — a verified multi-day continuity record for THIS story.

Built entirely from `story._continuity`, the dict news.agents.enrich()
attaches after the historian/verifier passes confirm a genuine continuation
(news.agents.continuity_facts) — this module invents nothing and does no
model or network I/O. A story needs at least MIN_OCCURRENCES verified past
appearances before the timeline is worth a panel (mirrors coverage.py's
"≥3 distinct hours" gate for its own timeline).

  history:timeline   the story's past occurrences, oldest matched first   → table
"""

from __future__ import annotations

MIN_OCCURRENCES = 3


def build(arg, panel, story):
    from . import SourceData, T_INT, T_STR

    continuity = getattr(story, "_continuity", None)
    if not continuity:
        return None
    occurrences = continuity.get("occurrences") or []
    if len(occurrences) < MIN_OCCURRENCES:
        return None

    rows = [[o["day"], o["headline"], o["importance"]] for o in occurrences]
    return SourceData(
        key=f"history-{story.slug}-timeline",
        columns=[{"name": "date", "type": T_STR},
                 {"name": "headline", "type": T_STR},
                 {"name": "importance", "type": T_INT}],
        rows=rows,
        mapping={"columns": "date,headline,importance"},
        panel_type="table",
        density="condensed",
        column_order=["date", "headline", "importance"],
    )
```

- [ ] **Step 4: Register it**

In `news/enrichers/__init__.py`, add the import (alongside the other
enricher imports) and the registry entry:

```python
from . import coverage as _coverage     # noqa: E402
from . import stocks as _stocks         # noqa: E402
from . import facts as _facts           # noqa: E402
from . import series as _series         # noqa: E402
from . import research as _research     # noqa: E402
from . import history as _history       # noqa: E402

# Prefix → builder. Add a new capability here (e.g. "sports": _sports.build).
# Headlines/summaries are not enrichers — run.py renders them into each story's
# markdown panel directly. Neither are images: they're unbound content panels.
REGISTRY = {
    "stock": _stocks.build,
    "coverage": _coverage.build,
    "facts": _facts.build,
    "series": _series.build,
    "research": _research.build,
    "history": _history.build,
}
```

In `news/plan_schema.py`, update `KNOWN_ENRICHERS`:

```python
KNOWN_ENRICHERS = {"stock", "coverage", "facts", "series", "research", "history"}
```

- [ ] **Step 5: Run to verify all pass**

Run: `./.venv/bin/pytest tests/test_enrichers_history.py -v`
Expected: 4 passed

- [ ] **Step 6: Run the full test suite**

Run: `./.venv/bin/pytest -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add news/enrichers/history.py news/enrichers/__init__.py news/plan_schema.py tests/test_enrichers_history.py
git commit -m "enrichers: history:timeline panel + registry wiring"
```

---

## Task 9: Weekly recap panel (`briefing.recap`)

**Files:**
- Modify: `news/enrichers/briefing.py`
- Create: `tests/test_briefing_recap.py`

**Interfaces:**
- Consumes: `news.history.{load_window, HistoryEntry, group_entries}`
  (Tasks 1/2/4).
- Produces: `news.enrichers.briefing.recap(plan, config) -> SourceData |
  None` — plan-scoped like `domain_mix`/`source_volume`, called directly by
  `run.py` (Task 10), not through `REGISTRY`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_briefing_recap.py`:

```python
from datetime import date
from types import SimpleNamespace

import pytest

from news import history
from news.enrichers import briefing


@pytest.fixture(autouse=True)
def isolated_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")


def _story(slug, headline, importance, domain="markets"):
    return SimpleNamespace(slug=slug, headline=headline, subject="", domain=domain,
                           importance=importance, breaking=False, sentiment="neutral",
                           summary="", _articles=[])


def _plan(day, stories):
    return SimpleNamespace(day=day, stories=stories)


def _config(lookback_days=7, max_stories=6, match_threshold=0.3):
    return {"history": {"match_threshold": match_threshold,
                        "recap": {"lookback_days": lookback_days,
                                 "max_stories": max_stories}}}


def test_recap_none_with_no_history_at_all():
    plan = _plan(date(2026, 8, 9), [])
    assert briefing.recap(plan, _config()) is None


def test_recap_includes_todays_stories():
    plan = _plan(date(2026, 8, 9), [_story("padres-win", "Padres win the series", 3)])
    sd = briefing.recap(plan, _config())
    assert sd is not None
    assert sd.panel_type == "table"
    assert sd.rows == [["2026-08-09", "Padres win the series", 3]]


def test_recap_collapses_a_continuation_chain_to_its_peak():
    history.write_day(date(2026, 8, 7),
                      [history.HistoryEntry(slug="fed-a", headline="Fed weighs a rate cut",
                                            subject="", domain="markets", importance=2,
                                            breaking=False, sentiment="neutral", summary="",
                                            article_count=1, entities=["Federal Reserve"])],
                      retention_days=60)
    history.write_day(date(2026, 8, 8),
                      [history.HistoryEntry(slug="fed-b", headline="Fed signals a rate cut",
                                            subject="", domain="markets", importance=4,
                                            breaking=False, sentiment="neutral", summary="",
                                            article_count=1, entities=["Federal Reserve"])],
                      retention_days=60)
    plan = _plan(date(2026, 8, 9),
                [_story("fed-c", "Fed cuts rates a quarter point", 3)])
    sd = briefing.recap(plan, _config(match_threshold=0.2))
    assert len(sd.rows) == 1
    assert sd.rows[0][1] == "Fed signals a rate cut"   # the peak-importance entry (4)


def test_recap_caps_at_max_stories_by_peak_importance():
    # Headlines deliberately share NO significant tokens with each other (no
    # common filler words either) so group_entries keeps all 7 as separate
    # groups — this test is isolating the sort/cap step, not the clustering
    # step (that's covered by test_recap_collapses_a_continuation_chain_to_its_peak).
    headlines = [
        "Wildfire spreads across the valley",
        "Chess championship ends in stalemate",
        "New bakery opens downtown",
        "Marathon route changes for construction",
        "Aquarium welcomes a baby otter",
        "Bridge repairs begin next month",
        "Local choir wins a regional award",
    ]
    # Importances 1..7 — real StorySpec clamps 1-5, but this SimpleNamespace
    # fixture bypasses that validation deliberately, to isolate the sort/cap
    # logic with unambiguous, distinct values.
    stories = [_story(f"s{i}", h, i + 1, domain="general")
              for i, h in enumerate(headlines)]
    plan = _plan(date(2026, 8, 9), stories)
    sd = briefing.recap(plan, _config(max_stories=3))
    assert len(sd.rows) == 3
    assert [r[2] for r in sd.rows] == [7, 6, 5]   # highest peak importance first
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_briefing_recap.py -v`
Expected: FAIL — `AttributeError: module 'news.enrichers.briefing' has no attribute 'recap'`

- [ ] **Step 3: Implement**

In `news/enrichers/briefing.py`, add at the end of the file:

```python
def recap(plan, config):
    """The past week's top stories, independent of today's news — a
    deterministic aggregation, no model call. A multi-day continuation chain
    (news.history.group_entries) counts once, at its peak importance, so a
    story that led for four straight days doesn't crowd out the rest of the
    week. Overview-board only."""
    from . import SourceData, T_INT, T_STR
    from .. import history as _history

    hist_cfg = config.get("history", {})
    recap_cfg = hist_cfg.get("recap", {})
    lookback = int(recap_cfg.get("lookback_days", 7))
    max_stories = int(recap_cfg.get("max_stories", 6))
    threshold = float(hist_cfg.get("match_threshold", 0.35))

    window = _history.load_window(plan.day, lookback)
    today_entries = [_history.HistoryEntry.from_story(s, plan.day.isoformat())
                     for s in plan.stories]
    all_entries = window + today_entries
    if not all_entries:
        return None

    groups = _history.group_entries(all_entries, threshold)
    peaks = [max(g, key=lambda e: e.importance) for g in groups]
    peaks.sort(key=lambda e: e.importance, reverse=True)
    top = peaks[:max_stories]
    if not top:
        return None

    return SourceData(
        key="briefing-recap",
        columns=[{"name": "date", "type": T_STR},
                 {"name": "headline", "type": T_STR},
                 {"name": "importance", "type": T_INT}],
        rows=[[e.day, e.headline, e.importance] for e in top],
        mapping={"columns": "date,headline,importance"},
        panel_type="table",
        density="condensed",
        column_order=["date", "headline", "importance"],
    )
```

- [ ] **Step 4: Run to verify all pass**

Run: `./.venv/bin/pytest tests/test_briefing_recap.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full test suite**

Run: `./.venv/bin/pytest -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add news/enrichers/briefing.py tests/test_briefing_recap.py
git commit -m "briefing: deterministic weekly recap panel"
```

---

## Task 10: `run.py` wiring — markdown note, write-gating, fatigue signal, recap panel

**Files:**
- Modify: `news/run.py`
- Create: `tests/test_run_history.py`

**Interfaces:**
- Consumes: `history.{HistoryEntry, write_day}` (Task 1),
  `briefing.recap` (Task 9), `story._continuity` (Task 7).
- Produces: `run.story_markdown` (existing, extended),
  `run._continuity_brief_fields(story) -> dict` (new, keys `days_running,
  trend`), `run.plan_to_dict` (existing, extended with a `continuity` key),
  `run.main` gains a history-write step gated on `not args.plan_only`.

- [ ] **Step 1: Write the failing tests for the pure/testable pieces**

Create `tests/test_run_history.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

from news import run


def _story(summary="The Fed cut rates.", articles=None, continuity=None):
    return SimpleNamespace(summary=summary, headline="Fed cuts rates",
                           _articles=articles or [], _continuity=continuity)


# ── story_markdown ───────────────────────────────────────────────────────────

def test_story_markdown_appends_continuity_note_when_present():
    continuity = {"note": "Fourth consecutive day of coverage."}
    md = run.story_markdown(_story(continuity=continuity))
    assert md.endswith("*Fourth consecutive day of coverage.*")


def test_story_markdown_omits_continuity_block_when_absent():
    md = run.story_markdown(_story(continuity=None))
    assert "*" not in md


def test_story_markdown_omits_continuity_block_when_note_empty():
    md = run.story_markdown(_story(continuity={"note": ""}))
    assert "*" not in md


# ── curator fatigue signal ───────────────────────────────────────────────────

def test_continuity_brief_fields_present():
    story = _story(continuity={"days_running": 4, "trend": "rising", "note": "x"})
    assert run._continuity_brief_fields(story) == {"days_running": 4, "trend": "rising"}


def test_continuity_brief_fields_defaults_when_absent():
    story = _story(continuity=None)
    assert run._continuity_brief_fields(story) == {"days_running": 0, "trend": ""}


# ── write-gating: history is written on a real run, never --plan-only ───────

def test_plan_only_never_writes_history():
    from datetime import date
    # `run.py` does `from .fetch import load_config`, which binds the name
    # directly into run's own namespace at import time — patching
    # `news.fetch.load_config` would NOT intercept `run.main`'s call to it.
    # Patch `run.load_config` (the name run.main actually calls) instead.
    # `plan.day` must be a real date (not None) — `plan_to_dict` calls
    # `plan.day.isoformat()` unconditionally in the --plan-only branch.
    fake_plan = SimpleNamespace(day=date(2026, 8, 9), stories=[])
    with patch.object(run, "build_plan", return_value=(fake_plan, [], {})), \
         patch.object(run, "history_write_day") as write_mock, \
         patch.object(run, "load_config", return_value={}):
        run.main(["--plan-only"])
    write_mock.assert_not_called()


def test_real_run_writes_history_before_apply_plan():
    fake_story = SimpleNamespace(slug="s", headline="h", subject="", domain="general",
                                 importance=3, breaking=False, sentiment="neutral",
                                 summary="", _articles=[])
    from datetime import date
    fake_plan = SimpleNamespace(day=date(2026, 8, 9), stories=[fake_story])
    # `new=lambda *a, **kw: None` (not `return_value=None`) — mocking an
    # async function with `return_value` leaves the awaited call unresolved,
    # producing a spurious "coroutine was never awaited" RuntimeWarning; a
    # plain callable substitute avoids it since `asyncio.run` is also mocked.
    with patch.object(run, "build_plan", return_value=(fake_plan, [], {})), \
         patch.object(run, "history_write_day") as write_mock, \
         patch.object(run, "apply_plan", new=lambda *a, **kw: None), \
         patch("asyncio.run"), \
         patch.object(run, "load_config", return_value={}):
        run.main([])
    write_mock.assert_called_once()
    called_day, called_entries, _retention = write_mock.call_args[0]
    assert called_day == date(2026, 8, 9)
    assert called_entries[0].slug == "s"
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_run_history.py -v`
Expected: FAIL — `AttributeError: module 'news.run' has no attribute
'_continuity_brief_fields'` (and similar for `history_write_day`)

- [ ] **Step 3: Import history and add the module-level alias**

In `news/run.py`, update the imports at the top:

```python
from . import enrichers
from . import history
from .agents import Ollama, curate, enrich, layout, sentiment_pass, timed, timings_report
from .enrichers import briefing
from .fetch import fetch_all, load_config
from .helio_client import HelioClient
from .history import HistoryEntry, write_day as history_write_day
from .plan_schema import DATA_PANEL_TYPES, DayPlan
```

(`history_write_day` is a module-level name specifically so tests can
`patch.object(run, "history_write_day")` without reaching into the `history`
submodule.)

- [ ] **Step 4: Extend `story_markdown`**

Replace the existing `story_markdown` function:

```python
def story_markdown(story) -> str:
    """One story's markdown panel body: summary + a linked headlines list +
    (when the historian/verifier passes confirmed one) a one-sentence
    continuity note. The section title is set on the panel, so it's
    deliberately NOT repeated here."""
    parts: list[str] = []
    if story.summary:
        parts.append(story.summary.strip())
    arts = getattr(story, "_articles", None) or []
    if arts:
        parts.append("\n**Headlines**")
        for a in arts[:6]:
            parts.append(f"- [{a.title}]({a.url}) — {a.source}")
    continuity = getattr(story, "_continuity", None)
    note = (continuity or {}).get("note", "")
    if note:
        parts.append(f"\n*{note}*")
    return "\n".join(parts) or story.headline
```

- [ ] **Step 5: Add the curator fatigue-signal helper and use it in `build_plan`**

Add a new small function right above `build_plan`:

```python
def _continuity_brief_fields(story) -> dict:
    """The curator fatigue signal for one story: {days_running, trend},
    both code-computed ground truth (news.history.ground_truth) — zeroed out
    when the historian/verifier passes didn't confirm a continuation."""
    continuity = getattr(story, "_continuity", None) or {}
    return {"days_running": continuity.get("days_running", 0),
           "trend": continuity.get("trend", "")}
```

In `build_plan`, update the `stories_brief` comprehension:

```python
    stories_brief = [{
        "slug": s.slug, "board": routing.get(s.domain, dcfg.get("overview", "News Overview")),
        "headline": s.headline, "subject": s.subject, "importance": s.importance,
        "breaking": s.breaking, "summary": s.summary,
        **_continuity_brief_fields(s),
    } for s in plan.stories]
```

- [ ] **Step 6: Feed the fatigue signal into the curator's prompt**

In `news/agents.py`, update `_CURATOR_SYS` (append a third bullet) and
`curate`'s line-rendering so the model actually sees the signal:

```python
_CURATOR_SYS = (
    "You are the editor-in-chief laying out today's news dashboards. You are "
    "given the day's stories, each already assigned to a section board. Do two "
    "things:\n"
    "1. Pick the {n} strongest stories overall for the front-page digest — the "
    "ones a reader must see first. Spread them across sections; don't take all "
    "from one. A story marked 'running Nth day' that ISN'T escalating (trend "
    "rising) is often a rehash — weigh a fresh lead story over a stale one at "
    "similar importance, though a big story can legitimately dominate for days.\n"
    "2. Write a ONE-sentence editor's brief for EACH board named below, framing "
    "what its stories add up to today (e.g. 'Washington is consumed by the "
    "shutdown fight while the Middle East ceasefire holds'). If a board has no "
    "stories, write a short 'quiet day' line for it.\n"
    'Return ONLY JSON {"overview":[slug,...],"briefs":{"<board>":"<sentence>"}} '
    "— use the board names EXACTLY as given."
)
```

And in `curate()`, extend the per-story line to include the signal when
present:

```python
    lines = []
    for s in stories_brief:
        flag = " BREAKING" if s.get("breaking") else ""
        running = f" (running day {s['days_running']}, {s.get('trend', '')})" \
            if s.get("days_running", 0) >= 2 else ""
        lines.append(
            f'- slug="{s["slug"]}" board="{s["board"]}" '
            f'importance={s.get("importance", 3)}/5{flag}{running}: {s.get("headline", "")}'
            + (f" — {s['summary'][:160]}" if s.get("summary") else "")
        )
```

- [ ] **Step 7: Add `continuity` to `plan_to_dict` (for `--plan-only` visibility)**

Update `plan_to_dict`:

```python
def plan_to_dict(plan: DayPlan) -> dict:
    return {
        "day": plan.day.isoformat(),
        "stories": [
            {**{k: v for k, v in asdict(s).items() if k != "panels"},
             "image": s.hero_image(),
             "facts": getattr(s, "_facts", []),
             "continuity": getattr(s, "_continuity", None),
             "panels": [asdict(p) for p in s.panels]}
            for s in plan.stories
        ],
    }
```

- [ ] **Step 8: Add the recap panel to the overview board**

In `apply_plan`, extend the day-in-review loop:

```python
        for sd, title in ((briefing.domain_mix(plan), "What kind of day"),
                          (briefing.source_volume(plan, articles), "Who's publishing"),
                          (briefing.recap(plan, config), "This week")):
```

(No other change needed in that loop — it already handles `sd is None` and
dedupes by `sd.key`.)

- [ ] **Step 9: Gate the history write in `main()`**

Replace `main()`'s body from `plan, articles, curation = build_plan(config)`
through the `if not plan.stories` check:

```python
    config = load_config()
    plan, articles, curation = build_plan(config)

    if args.plan_only:
        print(json.dumps({**plan_to_dict(plan), "curation": curation}, indent=2))
        print(timings_report(), file=sys.stderr)
        return 0

    if not plan.stories:
        print("No stories produced; nothing to build.", file=sys.stderr)
        return 1

    # Persist today's headlines for tomorrow's continuity matching — only on a
    # real run, never --plan-only (a dev loop re-running the same morning must
    # not fabricate multiple "days" of history).
    hist_cfg = config.get("history", {})
    history_write_day(plan.day, [HistoryEntry.from_story(s, plan.day.isoformat())
                                 for s in plan.stories],
                      int(hist_cfg.get("retention_days", 60)))

    asyncio.run(apply_plan(plan, articles, config, curation, cleanup=not args.keep))
```

- [ ] **Step 10: Run to verify the run.py tests pass**

Run: `./.venv/bin/pytest tests/test_run_history.py -v`
Expected: 7 passed

- [ ] **Step 11: Run the full test suite**

Run: `./.venv/bin/pytest -v`
Expected: all passed (every task's tests, ~44 total)

- [ ] **Step 12: Commit**

```bash
git add news/run.py news/agents.py tests/test_run_history.py
git commit -m "run: continuity note, curator fatigue signal, recap panel, history write-gating"
```

---

## Task 11: README + end-to-end manual verification

**Files:**
- Modify: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Add `history:timeline` to the Panel vocabulary table**

In `README.md`, in the "Panel vocabulary" table, add a row after the
`research:series` row:

```markdown
| `history:timeline` | this story's own multi-day record, historian-judged + verifier-audited | ≥3 verified past occurrences of the same ongoing story |
```

- [ ] **Step 2: Add `news/history.py` to the Layout table**

In the "Layout" table, add a row after `news/plan_schema.py`:

```markdown
| `news/history.py` | persistent day/week/month headline memory — store, candidate matching, day-count/trend ground truth |
```

- [ ] **Step 3: Note the new passes in the pipeline diagram section**

In the "Pipeline" ASCII diagram's pass list (the `1 triage ... 6 layout`
block), add a line after `3 critic`:

```
                                  3 critic    audit each figure against the source text
                                  3b historian/verifier  judge + audit multi-day continuity
                                  4 planner   pick panels from an offered MENU
```

- [ ] **Step 4: Document the `history:` config block under Extending**

At the end of the "Extending" section, add:

```markdown
- **Headline history:** `history:` in `outlets.yaml` controls retention
  (`retention_days`, default 60 — covers day/week/month buckets, filtered at
  read time from the raw day-files, not separately rolled up),
  `match_threshold` (how much keyword/entity overlap makes a past story a
  "candidate"), and the weekly recap's `recap.lookback_days`/`max_stories`.
  Stored under `state/history/` (gitignored), one JSON file per day, written
  only on real runs — never `--plan-only`.
```

- [ ] **Step 5: Run the full test suite one more time**

Run: `./.venv/bin/pytest -v`
Expected: all passed

- [ ] **Step 6: Manual end-to-end smoke test**

Run: `./.venv/bin/python -m news.run --plan-only`

Expected: exits 0, prints a valid JSON plan (each story has a `"continuity"`
key — `null` on a fresh install, since there's no stored history yet to
match against) and a timings report. Confirm no traceback.

Note: continuity notes / the `history:timeline` panel / the recap panel
won't have real data to show until `state/history/` has accumulated a few
real (non-`--plan-only`) days — that's inherent to what this feature is
memory of days that haven't happened yet on a fresh install. This is the
expected, correct behavior, not a bug to chase.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: document headline history in README"
```

---

## Post-implementation

After Task 11, the feature is code-complete and tested (pure logic) but its
model-facing behavior (historian/verifier prompt quality, whether the
curator actually uses the fatigue signal well) can only really be judged by
running the real pipeline for a few consecutive real days and eyeballing the
board — same as how `extract`/`critic`/`research` were validated in News
v2/v3. Consider a short follow-up note after a week of real runs on whether
`match_threshold: 0.35` is catching genuine continuations without false
positives (a coincidental same-domain, same-keyword story wrongly linked).
