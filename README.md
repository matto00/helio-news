# helio-news

A local, personal news aggregator that builds **content-shaped ("alive")**
dashboards in [helio](../helio) every morning. Feeds are pulled as RSS, a
sequence of local **model** passes (via ollama, all on `gpt-oss`) clusters and
interprets them, *extracts and fact-checks the story's key figures*, and *decides
which panels each story needs* — the result written to helio entirely through the
**helio MCP server** (Python is the MCP client — auth stays in the server, no REST
calls here).

> A breaking Nvidia story gets a price chart + a day/week/month trend bar. A
> Padres playoff story gets a photo and its headlines. A pure political story
> gets a photo, a summary, and who's covering it. The planner chooses per story.

## Pipeline

```
RSS (feedparser) ─► full text ─► model sequence (ollama, sequential) ─► enrichers ─► helio (MCP client)
  config/outlets    trafilatura  1 triage    cluster + domain + importance + breaking
                    (top 3/story) 2 extract   pull the story's key figures from the bodies
                    cached        3 critic    audit each figure against the source text
                                  3b historian/verifier  judge + audit multi-day continuity
                                  4 planner   pick panels from an offered MENU
                                  5 summarize subject + headline + summary
                                  6 layout    size every panel on the 12-col grid
```

Each built story's lead articles are hydrated to **full body text** (trafilatura,
cached in `state/bodies/`) before the model passes run — so summaries and the
figure extractor reason off substance, not RSS teasers. Any fetch that fails or
comes back thin degrades to the RSS summary; nothing depends on a scrape
succeeding.

Every pass runs on **one model** (`gpt-oss`, a 21B MoE) so nothing is evicted
mid-run — the passes interleave per story, and mixing models there would thrash
the 16 GB GPU. Each pass is still a separate ollama call with its own system
prompt and narrow input (far more reliable than one mega-prompt), and quality is
tuned per pass by **reasoning effort** (`reasoning:` in the config — `high` for
the judgement-heavy triage/extract/critic/curator, `low` for mechanical
sizing/tagging), not by swapping models.

### Three ideas do most of the work

**The planner is offered a menu, not a vocabulary.** `agents.story_offers()`
computes in code what data actually exists for a story — is there a photo? enough
outlets to chart? a ticker the news is moving? verified figures to tabulate? — and
the prompt lists only those, verbatim. A model asked to invent panel keys
hallucinates; the same model picking from real lines does well. Anything it emits
anyway is dropped in validation, so a bad plan degrades to summary-only rather
than breaking the run.

**The model sizes, the code packs.** The `layout` pass returns a `w × h` per
panel; `run._pack` flows those into non-overlapping grid positions. Asking a model
for 30 non-overlapping rectangles produces overlaps; asking it "how big should
this lead story be?" works. Judgement to the model, geometry to the code.

**Numbers are extracted, then adversarially fact-checked.** The `extract` pass
pulls the story's key figures out of the article bodies, each carried with the
verbatim sentence it came from; code then checks that quote is really in the text
(killing hallucinations deterministically), and a `critic` pass audits the
survivors — a figure reaches the "By the numbers" table only if its quote is found
*and* the critic agrees it states that value. Nothing invented, nothing rounded,
everything traceable to a source sentence.

## Layout

| Path | Role |
|------|------|
| `config/outlets.yaml` | feeds, watchlist/tickers, model-per-pass, stock gating, helio settings |
| `news/fetch.py` | RSS ingestion, lead-image extraction, full-text hydration (trafilatura, cached in `state/bodies/`), `--check` feed validator |
| `news/agents.py` | the model sequence (triage → extract → critic → planner → summarizer → layout) + sentiment/curator editorial passes |
| `news/plan_schema.py` | the planner contract; validates/repairs gemma output |
| `news/history.py` | persistent day/week/month headline memory — store, candidate matching, day-count/trend ground truth |
| `news/enrichers/` | pluggable aux-data: `stocks.py` (yfinance), `series.py` (contextual data), `coverage.py`, `briefing.py`, `facts.py` |
| `news/providers/` | real external data sources for `series:` — `fred.py` (economic series), `yahoo.py` (commodities/FX/crypto) |
| `news/helio_client.py` | MCP client wrapper (spawns the helio MCP server over stdio) |
| `news/run.py` | daily driver (`--plan-only`, `--keep`) + the grid packer |
| `deploy/` | systemd system service + wake-from-suspend timer |

## Panel vocabulary

Everything below is measured from real data — nothing on the dashboard is
invented by a model.

| Panel | Source | When |
|-------|--------|------|
| markdown (summary + headlines) | the clustered articles | every story |
| image | the widest photo any clustered article carries | planner's call; needs a feed that ships images |
| `facts:numbers` ("By the numbers", a grid of metric tiles) | figures extracted from the article bodies, each quote-grounded + critic-audited | ≥2 verified figures survive extraction |
| `stock:TICKER:1d\|1w\|1mo` | yfinance | **breaking** tech/markets stories only |
| `stock:TICKER:trend` | yfinance | day/week/month % change, as a bar |
| `stock:TICKER` (metric) | yfinance | latest price + day change |
| `series:<provider>:<id>[:monthly]` | a **real public dataset** (FRED / Yahoo) for a quantity the story is about — put on a trend line, captioned with its source; `:monthly` aggregates a dense daily series to a monthly average *in a helio pipeline* | a configured series (`series:` in config) is central to the story |
| `research:series` | a data series **an agent (Claude + web search) found** for the story from an authoritative public source — held to the fact panel's honesty bar (allowlisted domain + a verbatim quote re-verified against the source) | `research.enabled` + a lead/breaking story with no configured `series:` match, within the per-run budget |
| `history:timeline` | this story's own multi-day record, historian-judged + verifier-audited | ≥3 verified past occurrences of the same ongoing story |
| `coverage:sources` | the story's own articles | ≥3 outlets covering it |
| `coverage:timeline` | article timestamps | ≥3 distinct hours — shows a story breaking |
| briefing pie/bar/metrics | the day's fetch stats | always (the "at a glance" strip) |

Stocks are gated to breaking news (`stocks.breaking_only` in the config) — a
ticker earns a chart when the news is *moving* it, not every day.

## Setup

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# helio auth for the MCP server Python spawns (gitignored; never in config/code):
cat > .env <<'EOF'
HELIO_PAT=helio_pat_...
HELIO_API_BASE_URL=https://helio-backend-...run.app
# optional: enables FRED economic series (CPI, gas, unemployment…) in the
# `series:` enricher. Free key from https://fred.stlouisfed.org/docs/api/api_key.html
# Yahoo-backed series (oil, gold, bitcoin) work without any key.
FRED_API_KEY=...
# optional: enables the research agent (long-tail data series via Claude + web
# search). Only used when `research.enabled: true` in config; needs `anthropic`
# installed. Off by default.
ANTHROPIC_API_KEY=sk-ant-...
EOF

# ollama must be serving the models named in config/outlets.yaml:
ollama pull gemma4:e4b
```

The helio MCP server must include the **delete tools** (`delete_data_source`,
`delete_dashboard`, `delete_data_type`, `delete_panel`, …) — the daily
create-fresh / delete-old cleanup depends on them. Build it with
`cd ../helio/helio-mcp && npm run build`.

## Run

```bash
./.venv/bin/python -m news.fetch --check     # validate feed URLs
./.venv/bin/python -m news.run --plan-only    # fetch + gemma only; print plan JSON
./.venv/bin/python -m news.run                # build/refresh the helio dashboard
```

Schedule it: see `deploy/news.service` (installs as a systemd *user* timer).

## Extending

- **New feeds / interests:** edit `config/outlets.yaml` (`feeds`, `watchlist`).
  Note that a feed carrying **no images** (ESPN, CNBC, TechCrunch, Al Jazeera)
  can still contribute to an illustrated story — the photo is taken from whichever
  clustered article has the widest one. `--check` reports per-feed image coverage.
- **New panel kind** (e.g. sports rosters): add `news/enrichers/sports.py`
  exposing `build(arg, panel, story) -> SourceData`, register its prefix in
  `enrichers/REGISTRY` **and in `plan_schema.KNOWN_ENRICHERS`**, then offer its
  key from `agents.story_offers()` — only offer it when the data really exists
  for that story, since the planner can only pick from what it's shown. Nothing
  else changes: an unknown or failed enricher drops that one panel and the story
  keeps its summary.
- **Route a pass to a bigger model:** change one line under `models:` in the
  config. The `layout` pass is the cheapest one to upgrade — it runs once per
  run, not once per story.
- **Want stocks back every day:** set `stocks.breaking_only: false`.
- **Headline history:** `history:` in `outlets.yaml` controls retention
  (`retention_days`, default 60 — covers day/week/month buckets, filtered at
  read time from the raw day-files, not separately rolled up),
  `match_threshold` (how much keyword/entity overlap makes a past story a
  "candidate"), and the weekly recap's `recap.lookback_days`/`max_stories`.
  Stored under `state/history/` (gitignored), one JSON file per day, written
  only on real runs — never `--plan-only`.

### Gotchas worth knowing

- **Chart types need a full appearance object.** Setting `chartType` via
  `update_panel_appearance` requires resending a *complete* `ChartAppearance`;
  a partial `{"chartType": "bar"}` is rejected with a 400. See
  `helio_client.CHART_APPEARANCE`.
- **`config.chartOptions` is keyed BY CHART TYPE.** The display options
  (`{smooth,areaFill}` for line, `{orientation,stacking}` for bar,
  `{donutHolePct}` for pie) must be nested under the chart-type key —
  `{"line": {...}}`, not a flat `{...}`. A flat dict is **silently dropped**
  (no error, options just don't apply). `SourceData.panel_config()` nests them
  under `chart_type` for you.
- **`collection` / `timeline` panels: use `create_panel`, not the proposal
  path.** The built `dist` MCP's `apply_proposal` schema still enumerates
  `divider` but not `collection`/`timeline` (HEAD source has them — schema drift
  in the deployed build). `facts:numbers` renders as a `collection` of metric
  tiles via `create_panel` + `bind_panel(panelType="collection")`.
- **Image panels are unbound** — `config: {imageUrl, imageFit}`, no source or
  pipeline. `imageFit` ∈ `contain|cover|fill`.
- **Feeds advertise the smallest image first.** The Guardian lists 140/460/700px
  in that order, and its URLs are signature-signed so the width *cannot* be
  rewritten — the widest variant must be selected. The BBC ships only 240px
  thumbnails but its CDN serves `/800/` at the same path (unsigned), so those are
  upgraded instead. See `fetch._entry_image`.
