# Headline history — design

Status: **design / spec** (not yet built). Extends the News v2/v3 pipeline
(`news/`). Adds day/week/month memory of past headlines and uses it to inform
how today's stories are presented — continuity notes, a fatigue signal for the
curator, and a weekly recap section. New `historian` + `verifier` model passes
follow the same generative-then-adversarially-audited shape as the existing
`extract`/`critic` pair.

## Thesis

Today every run is stateless: `state/bodies/` only caches article text, and
`plan.stories` are rebuilt from scratch each morning with no memory of what ran
yesterday or last week. A story that's been developing for four days looks
identical to one that broke five minutes ago. This adds a persistent, bounded
memory of past headlines and uses it to answer three questions the board can't
answer today:

- **Is this story still developing, and since when?** (continuity note, and a
  small timeline panel for real multi-day arcs)
- **Should the curator be skeptical of a rehash?** (a `days_running`/`trend`
  signal fed into the existing curator pass)
- **What mattered this week, independent of today's news?** (a recap section
  on the Overview board)

## The honesty invariant (carried over from News v3)

Same non-negotiable line as the rest of the pipeline: nothing on the board is
model-invented.

- **Day counts and dates are never left to a model.** They're arithmetic over
  the stored record (candidate list length, min/max date) computed in code —
  the model never gets to assert "day 4" on its own authority.
- The **historian** pass is the one judgement call that genuinely needs a
  model: is a topically-similar past story actually the *same* ongoing story,
  or coincidental overlap? That's the same kind of call `triage` already makes
  when clustering same-event articles.
- The **verifier** pass audits that judgement adversarially (mirrors
  `critic_numbers`'s relevance-judge role) and cross-checks the historian's
  prose against the code-computed ground truth. A mismatch drops the
  continuity data for that story; the story still renders normally, same
  fail-soft posture as every other enricher.

## Storage

One JSON file per day: `state/history/YYYY-MM-DD.json`, written after
`enrich()` produces the plan — before the helio push, so a downstream MCP
failure never loses the day's record. **Only written on real runs**, never
`--plan-only` (a dev loop shouldn't pollute history with repeated re-runs of
the same morning).

Per story, the minimum needed for matching + arithmetic:

```json
{
  "slug": "fed-rate-cut",
  "headline": "Fed cuts rates a quarter point",
  "subject": "Federal Reserve rate policy",
  "domain": "markets",
  "importance": 4,
  "breaking": false,
  "sentiment": "neutral",
  "summary": "...",
  "article_count": 5,
  "entities": ["Federal Reserve"]
}
```

`entities` is the union of `Article.matched` (watchlist hits) across the
story's clustered articles — no `StorySpec` schema change needed to produce
it; it's computed at write time from the story's `_articles`.

**Retention:** `history.retention_days` in config, default **60** (covers the
month bucket with real headroom). Old day-files beyond the window are pruned
on write. Day/week/month are **not** separate rollup stores — they're filters
over the raw daily files at query time (last 1 / 7 / 30 days). Simpler, no
rollup-drift bugs, and the only cost is a handful of small JSON file reads.

New module: `news/history.py` — owns the store (read/write/prune) and the
matching function. Kept separate from `agents.py` per the existing
convention (`providers/`, `enrichers/` are already split out by
responsibility).

## Matching (code, no model call)

For each of today's stories, build a token set from `headline + subject`
(stopwords stripped) plus `entities`, and score overlap against every stored
story in the retention window, weighted toward recency. Stories above
`history.match_threshold` (default `0.35`) become **candidates** — this is
deterministic, same spirit as `_central_tickers`/`_central_series`'s keyword
gating.

A story with **zero candidates** costs nothing further — no historian call,
no verifier call. On most days, most stories have no history to match, so the
added run cost is close to zero.

## Historian pass (gemma, gated per-story)

Runs only for stories with ≥1 candidate. Input: today's headline/subject/
summary + the candidate list (past dates, headlines, importance — real stored
data, not invented). Output:

```json
{
  "is_continuation": true,
  "trend": "rising",
  "note": "Fourth consecutive day of coverage, escalating from a routine update to today's lead story."
}
```

`is_continuation` is the semantic judgement code can't make (are these
really the same story, not just the same keyword). `trend` is the model's
read of rising/falling/steady importance — cross-checked against the
code-computed importance delta by the verifier, not trusted blind.

Reasoning effort: **medium** (a real but narrow judgement call, not as hard
as triage clustering across the whole day's candidate pool).

## Verifier pass (gemma, gated on historian confirming)

Same adversarial-audit role `critic_numbers` plays for extracted figures:

- Re-examines `is_continuation` skeptically — **defaults to rejecting when
  uncertain**, same posture as the numbers critic.
- Checks the note's stated claims (day count, direction of trend) against the
  code-computed ground truth (candidate count/dates, importance delta). Any
  claim in the note that doesn't match the real numbers → reject.

Rejection at either check drops `_continuity` for that story entirely; the
story renders with no continuity note/panel, same as a failed enricher today.

Confirmed output is stashed as a dynamic attribute, `story._continuity`,
following the existing `_articles`/`_facts` pattern — no `StorySpec` dataclass
changes.

```python
story._continuity = {
    "is_continuation": True,
    "days_running": 4,          # code-computed
    "first_seen": "2026-08-06", # code-computed
    "trend": "rising",
    "note": "Fourth consecutive day of coverage...",
    "occurrences": [...],        # past (date, headline, importance) — for the timeline panel
}
```

## Surfacing

- **Story markdown panel:** the verified `note` is appended below the
  headlines list — one sentence doesn't earn its own panel (per existing
  "no panel for brief text" practice).
- **`history:timeline` panel** (new enricher, registered in `REGISTRY` +
  `KNOWN_ENRICHERS`): only when `len(occurrences) >= 3` — mirrors
  `coverage:timeline`'s "≥3 distinct hours" gate. A small table: date,
  headline, importance.
- **Curator fatigue signal:** `days_running` and `trend` are added to the
  `stories_brief` dicts already passed into `curate()` in `run.build_plan`
  (no new pass — the curator already takes structured per-story input). Lets
  the curator deprioritize/note a rehash in its brief.
- **Weekly recap section (Overview board):** deterministic, no model call.
  Pulls the week bucket (`history.recap.lookback_days`, default 7), collapses
  continuation-chains to one entry each (at peak importance), takes the top
  `history.recap.max_stories` (default 6), renders as a `collection` panel —
  same aggregation style as `briefing.py`'s day-in-review panels.

## Pipeline placement

`triage → extract → critic → planner → summarizer → **historian/verifier
(new)** → sentiment → curator → layout`

Placed after `summarizer` (needs the final headline/subject) and before
`sentiment`/`curator` (curator needs `days_running`).

## Config

New `history:` block in `outlets.yaml`:

```yaml
history:
  retention_days: 60
  match_threshold: 0.35
  recap:
    lookback_days: 7
    max_stories: 6
```

## Cost

Two extra small gemma calls, but only for stories with a code-side candidate
— on a typical day this is a handful of stories at most, most days zero.
Storage cost is trivial (small JSON files, pruned at 60 days).

## Open questions

None outstanding — design approved section-by-section during brainstorming.
