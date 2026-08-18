# Project pulse — Linear + git activity dashboards (design)

## Status (2026-08-18)

Approved design, not yet implemented. Next step: `superpowers:writing-plans`.

## Context

The user runs several independent projects (`helio`, `concertino`, more as they
start) tracked in Linear. They want a daily "pulse" on each — what shipped,
how fast, what's piling up — built the same way the news dashboards are:
fetched data, a local-model narrative pass, and real helio panels, refreshed
daily and unattended.

This is **not** the news pipeline's story-clustering problem. There's no
RSS noise to dedupe, no ambiguous "is this the same story" judgment call — a
Linear ticket is already a discrete, labeled unit. So this doesn't reuse
`agents.py`'s triage→extract→critic→planner chain; it's structurally closer
to `enrichers/series.py` (fetch real external data, let helio's pipelines do
the arithmetic, caption everything with its real source).

## Scope

**In scope:**
- New `news/projects/` module: fetch (Linear + git log) → CSV upload → helio
  pipeline aggregation → LLM narrative → one dashboard board per project.
- Runs as one more phase inside the existing daily `news.run` job — same
  timer, same `gpt-oss` model, same `HelioClient`.
- Two projects at launch: `helio` (Linear team "Helio Platform"), `concertino`
  (Linear team "Concertino"). Config-driven list (`projects.items` in
  `config/outlets.yaml`) so a new project is a config entry, not a code change.
- Metrics: velocity (ticket throughput, trended over time), cycle time
  (start→done duration), backlog health (open bug count, oldest open ticket).

**Explicitly out of scope:**
- Commit/PR volume as its own metric panel (git log content feeds the LLM
  narrative only — the user declined it as a separate tracked metric).
- The third existing Linear team, "Helio IOT" — no confirmed local repo
  backing it yet. Add via config later if/when one exists.
- Any change to the news pipeline's own boards/passes.

## Why helio pipelines do the math, not Python

The user was explicit: upload raw data, let helio's pipelines compute the
metrics — not pre-aggregate in Python and upload finished numbers. This was
verified live against the real backend (throwaway probes, created + deleted,
zero residue) before committing to it:

- **`create_csv_data_source` + `time-series` pipeline shape**
  (`datebucket` → `aggregate` → `sort`, one MCP call via
  `create_pipeline_from_shape`) correctly computes `avg`/`count` measures
  over CSV-sourced numeric columns, even though those columns arrive as
  strings internally. Confirmed: a CSV of `{id, project, completedAt,
  cycleTimeDays}` rows, bucketed by week with `avg(cycleTimeDays)` and
  `count(id)` measures, produced correct per-week averages.
- **The `compute` step cannot do row-level date arithmetic on CSV-sourced
  data.** `$completedAt - $startedAt` silently evaluates to `null` for every
  row (no `validationError` — this is `ExpressionEvaluator.evaluate`'s
  documented "type error → null" behavior, per
  `openspec/specs/pipeline-compute-op/spec.md`). Isolated via a control
  test: the identical expression against a `create_data_source` (native JSON
  int) source computes correctly. So the gap is specifically CSV string
  values reaching `compute`'s strict-numeric evaluator, not `compute` or
  arithmetic in general.

Given that, the design draws the line at: **Python computes exactly one
derived-but-still-per-row field where the pipeline can't** (`cycleTimeDays`,
`ageDays` — both a single date subtraction), uploads them as ordinary CSV
columns alongside the raw ticket data, and every actual *statistic* —
averaging, counting, week-bucketing, sorting/limiting — happens in a helio
pipeline. This isn't a workaround of the user's intent; it's the smallest
concession to a confirmed, narrow tool gap, called out explicitly so it
isn't mistaken for scope creep later.

## Data flow

```
config/outlets.yaml: projects.items = [{name, linear_team, repo_path}, ...]
  │
  ├─ news/providers/linear.py — direct GraphQL client (api.linear.app/graphql),
  │  LINEAR_API_KEY-gated, fail-soft like fred.py/yahoo.py. Two queries per
  │  project:
  │    1. completed tickets, updatedAt within lookback_days (90d default) —
  │       for velocity + cycle time
  │    2. all currently-open tickets, NO date filter — for backlog health
  │       (a stale 6-month-old bug must still show up as "oldest open")
  │
  ├─ git log (subprocess, local repo_path, `main`, `--since=<narrative_days>`)
  │  — subject lines only, feeds the LLM narrative, not a metrics panel
  │
  ├─ Python: build two CSVs per project (ageDays/cycleTimeDays precomputed,
  │  everything else passed through raw)
  │
  ├─ HelioClient: create_csv_data_source → create_pipeline_from_shape →
  │  run_pipeline → create_panel/bind_panel (existing granular chain —
  │  unaffected by the HEL-644 create_bound_panel block, since shapes only
  │  build the pipeline; binding still goes through the working path)
  │
  └─ one gpt-oss pass per project: completed-ticket titles (narrative_days
     window) + git log subjects → "what shipped" paragraph
```

## Per-project board

One board per project (mirrors the news section-board pattern), 5 panels,
rebuilt daily via the existing create-fresh cleanup+rebuild cycle
(`clear_dashboard_panels` + `cleanup_news_resources`, already made
best-effort-per-resource as of the 2026-08-18 outage fix):

| Panel | Type | Pipeline |
|---|---|---|
| "What shipped" | markdown | — (LLM narrative, not pipeline-derived) |
| Velocity trend | chart | `time-series` shape: `timeField=completedAt`, `granularity=week`, `measures=[count(id)]` |
| Avg cycle time | metric | `single-row` shape: `mode=aggregate`, `avg(cycleTimeDays)` |
| Open bug count | metric | `single-row` shape: `mode=aggregate`, `count`, filtered to open+bug (exact `filter` step config confirmed during implementation via live probe, following this project's existing convention) |
| Oldest open tickets | table | `top-n` shape: sort `ageDays` desc, `n=backlog_top_n` (5 default) |

## CSVs

Two per project per day, not one — open tickets have no `completedAt`/
`cycleTimeDays`, so a single shared schema would carry structural nulls.
Honest separate schemas instead:

- `tickets_completed`: `id, title, completedAt, cycleTimeDays`
- `tickets_open`: `id, title, priority, isBug, createdAt, ageDays` —
  `isBug` is `true` iff the ticket carries a Linear label named exactly
  `"Bug"` (case-sensitive match against `labels`, same convention HEL-644
  itself uses)

Both are named/prefixed so they fall inside `cleanup_news_resources()`'s
existing sweep (`news-*-src-*` pattern) — no new cleanup logic needed, same
daily create-fresh lifecycle as everything else this pipeline builds.

## Config

```yaml
projects:
  enabled: true
  lookback_days: 90     # velocity + cycle-time window
  narrative_days: 7     # "what shipped" rolling window (kept short and
                         # separate from lookback_days so a quiet week
                         # doesn't produce an empty narrative panel)
  backlog_top_n: 5
  items:
    - name: "Helio"
      linear_team: "Helio Platform"
      repo_path: "/home/matt/Development/helio"
    - name: "Concertino"
      linear_team: "Concertino"
      repo_path: "/home/matt/Development/concertino"
```

`.env` gains `LINEAR_API_KEY` (already added — the user has one provisioned
for their concertino setup; reused here, same key, same variable name).

## Error handling

Fail-soft per project, matching the news pipeline's existing philosophy — a
quiet board already renders "0 panels / 0 stories" today with no special
casing (`Markets & Business: 0 panels / 0 stories`, seen in production this
same day). If Linear or git log fetch fails for one project: log a warning
to stderr, skip that project's board rebuild for the day. Must not abort the
other project's board or any news board. A missing `LINEAR_API_KEY` disables
the whole `projects` phase the same way an absent `FRED_API_KEY` disables
FRED series today — warn once, skip, don't crash the run.

## Testing

- Pure functions get unit tests: `cycleTimeDays`/`ageDays` computation, CSV
  row construction from parsed Linear issues, git log subject parsing.
- `HelioClient` calls (if `projects.py` needs anything beyond the existing
  `build_bound_panel`/`clear_dashboard_panels` methods) get stub-based tests
  following `tests/test_helio_client.py`'s pattern (hand-rolled async stub,
  no new test dependency).
- `providers/linear.py` gets a one-time live smoke test during
  implementation, not a mocked-HTTP unit test — matching `fred.py`/
  `yahoo.py`, which have neither today.
- Live smoke test (manual, same convention as v3/v4): after implementation,
  build one project's board against real data, confirm all 5 panels render
  with correct values, tear down if it was a scratch run.

## Non-goals

- No change to the news pipeline's own triage/extract/critic/planner chain
  or its boards.
- No commit/PR-volume panel (git log is narrative fuel only).
- No support for the "Helio IOT" Linear team until a local repo exists for
  it.
- No retry/backoff logic for Linear API failures beyond the existing
  fail-soft-and-skip pattern — matches how `fred.py`/`yahoo.py` behave today.
