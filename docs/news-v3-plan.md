# News v3 — contextual data + v1.5 panel parity

Status: **plan / design doc** (not yet built). Supersedes nothing; extends the
News v2 pipeline (`news/`). Written against helio-mcp `release/v1.5`.

## Thesis

The board already answers three questions about a story:

- **what happened** — the summary markdown,
- **who's covering it** — the `coverage:*` panels,
- **the story's own numbers** — the grounded `facts:numbers` panel.

v3 adds a fourth axis — **put the story in context**. A story names a
*quantity* (inflation, gas, eggs, unemployment, oil, box office, a team's
record). We pull the **real trend of that quantity from an authoritative
dataset**, aggregate it with a helio pipeline to the fidelity that fits, and
show it as a chart/table whose caption ties it to the news. Plus a v1.5 polish
pass so every existing panel uses the new config surface (collection tiles,
captions, chart display options).

This is the fix for "table panels are aggregations of multiple metrics and feel
lack-luster": we stop hand-packing rows and instead **upload real data once and
let pipelines shape it** into several panels at different fidelities.

## The honesty invariant (non-negotiable)

The pipeline's whole differentiator is that **no number on the board is
model-invented** — facts are quote-grounded + critic-audited, stocks are real
yfinance, coverage is derived from the articles. v3 must hold the same line:

> Every value traces to a real fetched source. A model may decide *which*
> dataset is relevant and *how* to frame it, but it never authors the numbers.

Consequences that shape the design:
- Data comes from **provider fetches or fetched web pages**, never from a
  model's memory. Fetch-or-drop everywhere (same as the body-hydration gate).
- Every contextual panel carries **provenance** in its caption (source name +
  URL / series id). If it can't be attributed, it doesn't ship.
- The research subagent (P3) is held to the *same* extract→ground→critic
  discipline as `facts` — it extracts published figures with their source, and
  a critic audits relevance + that values were copied, not computed.

## What v1.5 gives us (confirmed config shapes)

Read straight from `helio-mcp/src/tools/write.ts` + `proposal.ts`:

| Capability | Config shape | News use |
|---|---|---|
| `collection` panel | create: `config.baseType:"metric"`, `config.layout:"grid"\|"list"`; bind `panelType:"collection"`, `fieldMapping:{value,label?,unit?}` per row | KPI-tile strip — replaces the weak `facts:numbers` 2-col table |
| Image caption | `config.caption` (static string) | Photo credit: `"{headline} — {outlet}"` |
| Chart annotation | `config.annotation` (static) **or** `fieldMapping.annotation` (bound column, first-row value; static wins) | **Footnote/subtitle**, not an on-plot marker. Source attribution + event framing under a chart |
| Chart display opts | `config.chartOptions` — line `{smooth,showPoints,areaFill}`, bar `{orientation,stacking,barGapPct}`, pie `{donutHolePct,showPercentLabels}`, scatter `{sizeField,colorField}` | donut domain-mix, horizontal coverage bars, smooth+area series lines |
| Table config | `config.density` (condensed\|normal\|spacious), `config.columnOrder` | condensed, ordered facts/coverage tables |
| `timeline` panel | `config.timelineOptions.sort` | breaking-story development sequence (P1 stretch — verify MCP support first) |
| CSV source | `create_csv_data_source(name, content)` | ingest external series as CSV |
| `upload_image` | returns `{id,url,markdownRef}` | host hero images — **but see caveat below** |
| `apply_proposal` | `(dashboardName, panels[])` — atomic, validated | batch board build — **but see caveat below** |

## Architecture — the "context" axis

### The `series:` enricher

New `news/enrichers/series.py`, same contract as `stocks.py`
(`build(arg, panel, story) -> SourceData | None`, registered in
`enrichers/REGISTRY` **and** `plan_schema.KNOWN_ENRICHERS`). Panel data key:
`series:<provider>:<id>[:<transform>]`, e.g. `series:fred:GASREGW`,
`series:yahoo:CL=F:yoy`.

The enricher never fabricates: it calls a provider adapter, gets a real series +
a source URL, and returns `SourceData` carrying the rows **plus** the pipeline
steps that shape it (see fidelity below) and the caption/attribution.

### Provider adapters (`news/providers/`)

Thin, each returns `(rows, columns, source_url, attribution)`:

- **`fred.py`** — St. Louis Fed FRED API (free key, `FRED_API_KEY` in `.env`).
  The workhorse: CPI/inflation (`CPIAUCSL`), gas (`GASREGW`), eggs
  (`APU0000708111`), unemployment (`UNRATE`), mortgage rates (`MORTGAGE30US`),
  etc.
- **`yahoo.py`** — factor the existing `stocks.py` yfinance calls out here and
  extend to **commodity futures / FX / crypto** (`CL=F` crude, `GC=F` gold,
  `ZW=F` wheat, cattle, `BTC-USD`). `stocks.py` becomes a thin caller.
- **`owid.py` / world-bank** (later) — CSV-over-HTTP, no key, for world/health/
  energy series.

Every adapter is fetch-or-drop and returns a citable URL.

### Story → series mapping

Two mechanisms, layered (start with the first — it's the safe one):

1. **Config series-map** (like the ticker `watchlist`). New `outlets.yaml`
   section maps keywords → provider+id+transform. Deterministic and always
   correct; gated for centrality exactly like `_central_tickers` (keyword in the
   headline or an article title, not a loose body match):

   ```yaml
   series:
     - { name: "US inflation (CPI)", keywords: ["inflation","cpi","consumer price"],
         provider: fred, id: CPIAUCSL, transform: yoy }
     - { name: "Gas prices",  keywords: ["gas price","gasoline","pump price"],
         provider: fred, id: GASREGW }
     - { name: "Crude oil",   keywords: ["oil price","crude","opec","barrel"],
         provider: yahoo, id: "CL=F" }
   ```

2. **Model-proposed id, validated by fetch** (later, optional). A small pass
   proposes a FRED series id for a story; we *actually fetch it* and drop if it
   fails or returns nothing. More flexible, needs the validation gate so a bad
   guess can't render a confident-but-wrong chart.

Offered to the planner through `agents.story_offers()` like everything else —
only when a configured series matches, and gated (importance/breaking or domain)
like stocks, so a passing mention never spawns a chart.

### Pipeline aggregation = "varying fidelity"

Today `helio_client.build_bound_panel` only ever adds an **identity `select`**.
v3 uses the real steps (`groupBy`, `aggregate`, `compute`, `sort`, `limit`) so
one uploaded series produces panels at different fidelities:

- daily series → `groupBy(month) + aggregate(avg)` → monthly trend line;
- `compute` a YoY % column → a "how much has X changed" bar;
- `sort(date desc) + limit(12)` → a last-12-months table.

`SourceData` gains an optional `steps: list[dict]` (default: today's identity
select). `build_bound_panel` appends them and uses `analyze_pipeline` to confirm
the output columns before binding. This is the mechanism the user asked for:
**data lives once, pipelines shape it.**

### The research subagent (P3, long tail)

For a story with a quantifiable trend but **no** configured series, a gated,
grounded subagent:
1. decides if there's a real trackable quantity worth showing;
2. web-searches an **authoritative** source (statistical agency / reputable
   dataset), from a domain allowlist;
3. extracts the **published** series *with its source URL and verbatim figures*
   — never synthesizing numbers;
4. is audited by a critic (relevance + values-copied-not-computed) exactly like
   `facts`; reject → drop.

It emits the same `SourceData` shape as `series.py`, so pipeline/chart/caption
plumbing is shared. **Gated hard**: lead/breaking stories only, N-per-run
budget, cached by `slug+day` — a per-story web agent otherwise blows the ~20-min
run budget.

## Phase plan

### P1 — adopt v1.5 primitives (no new data) — ✅ DONE (verified against live MCP)

Low risk, immediate visible upgrade to every board. Also builds the plumbing P2
reuses (collection binding, captions, chart options). All shapes probe-verified
on the live MCP (throwaway board, created + deleted).

1. ✅ **`facts:numbers` → `collection` tiles.** `enrichers/facts.py` emits
   `panel_type="collection"` (+ `base_type="metric"`, `layout="grid"`,
   `mapping:{label,value}`); `SourceData.panel_config()` + `build_bound_panel`
   create with `config:{baseType,layout}` and bind `panelType:"collection"`.
   `collection` added to `run._FALLBACK`/`_BOUNDS` + the layout prompt.
2. ✅ **Hero captions.** `add_image_panel(caption=…)` → `config.caption =
   "{headline} — {outlet}"`, wired in `run._build_story`.
3. ✅ **Chart display options.** `SourceData.chart_options` → `config.chartOptions`
   **nested under chart_type** (a flat dict is silently dropped — see below).
   domain-mix pie → donut; `coverage:sources` + `briefing:sources` → horizontal
   bar; stock line → `smooth+areaFill`.
4. ✅ **Chart annotation as source attribution.** `SourceData.annotation` →
   `config.annotation`; set to `"Source: Yahoo Finance"` on stock charts (the
   provenance footnote; the mechanism P2's series charts reuse).
5. ✅ **Table density/order.** `coverage:sources` table → `condensed` +
   `columnOrder`.
6. ⏭️ **`timeline` panel** deferred — the deployed `dist` MCP's schema still
   omits it (drift from HEAD); revisit when the build catches up.

**Learnings folded into README gotchas:** `chartOptions` is keyed *by chart
type* (flat → silently dropped); `collection`/`timeline` go through
`create_panel`, not the proposal path, on the current `dist`.

### P2 — series enricher + adapters — ✅ DONE (verified end-to-end against live MCP)

1. ✅ `news/providers/{__init__,fred,yahoo}.py` — a `Series` dataclass carrying
   provenance (`source`/`url`); `yahoo.fetch` (keyless, commodities/FX/crypto/
   equities) and `fred.fetch` (gated on `FRED_API_KEY`, returns None without it).
   *(stocks.py left on its own yfinance calls for now — a later cleanup can
   route it through `providers/yahoo.py`; not needed for P2.)*
2. ✅ `news/enrichers/series.py` — `series:<provider>:<id>[:monthly]`; registered
   in `REGISTRY` + `KNOWN_ENRICHERS`.
3. ✅ `outlets.yaml` `series:` map (8 series: CPI, gas, unemployment, mortgage,
   eggs, oil, gold, bitcoin) + `agents._central_series()` centrality gate
   (headline/title keyword), sports domain excluded.
4. ✅ `story_offers()` emits `series:*` offers when a configured series is central.
5. ✅ `SourceData.steps` + `pipeline_steps()`; `build_bound_panel` loops the steps
   (default identity select). `:monthly` runs a real `aggregate` (groupBy month →
   avg) + `sort` — daily→monthly fidelity reduction **in the pipeline**.
6. ✅ Annotated (`Source: FRED/Yahoo — <id>`) + smooth/area contextual line
   (reuses P1 chart config).

**Verified:** live yahoo fetch (252 daily crude points); the aggregation shape on
the real MCP (5 rows → 3 monthly); and the *whole* path through the pipeline's
own `HelioClient` — panel bound to `avg_value`, a column that exists only after
the aggregate step, proving the multi-step pipeline ran. All probes cleaned up
(zero residue). FRED path needs a free `FRED_API_KEY` in `.env` to activate.

### P3 — research agent (gated, grounded) — ✅ DONE (off by default)

Built as a **Claude + web-search** call rather than a Claude Code subagent —
the daily run is an unattended Python process on local ollama, so the "agent"
had to be something it can invoke at 07:00: the Anthropic SDK with the
`web_search_20260209` server tool.

1. ✅ `news/providers/research.py` — `research_series(story, bodies, config)`:
   one `client.messages.create` (adaptive thinking + web search, `pause_turn`
   resumed), returns a `Series` or None. Imports `anthropic` lazily; no key/SDK →
   warns once, returns None.
2. ✅ **Honesty gate = the facts discipline, applied to the web:** the cited
   `source_url` host must be on `research.domains` (authoritative allowlist)
   **and** a verbatim `quote` the agent returns must be found in a re-fetch of
   that source (`agents._grounded`). Either check failing drops the panel — a
   fabricated series with an invented citation can't survive.
3. ✅ `news/enrichers/research.py` (`research:series`) formats the stashed,
   already-verified `Series` — same annotated line as `series:`. Registered in
   `REGISTRY` + `KNOWN_ENRICHERS`; offered via `story_offers(research_label=…)`.
4. ✅ Gated in `enrich()` by `agents._wants_research`: only when enabled, **no
   configured series matched**, lead/breaking, non-sports, within
   `research.max_per_run`. Provenance caption mandatory (from the `Series`).
5. ✅ Config `research:` block (**`enabled: false`** default, model, budget,
   min-importance, domain allowlist); `anthropic` added to `requirements.txt`
   (lazy — pipeline runs without it); `ANTHROPIC_API_KEY` documented in `.env`.

**Verified:** the gate logic offline (allowlist accept/reject, tolerant JSON
parse, point coercion, disabled→None, quote-grounding accept/reject) and the
render path (research `SourceData` == the P2 series shape already proven live).
The billable Claude call itself is exercised only when the user enables it — by
design; the honesty-critical code is what's tested.

## Cross-cutting

- **Cleanup.** `cleanup_news_resources` already sweeps `news-*` sources/types —
  CSV sources created via `create_csv_data_source` must match the same
  name prefix so they're swept too.
- **Config/secrets.** `FRED_API_KEY` in the gitignored `.env` (never in
  `outlets.yaml`/code), same as `HELIO_PAT`.
- **Verification.** Extend `--plan-only` to print `series:*` offers per story;
  add a `--probe` for adapters (fetch one series, print rows + URL); eyeball one
  board before scheduling.

## Confirmed constraints & gotchas

- **`apply_proposal` creates, it does not refresh.** It takes `dashboardName`
  (not id) and builds a *new* dashboard atomically — adopting it would change
  board ids every run and break the stable-board model. **Decision: keep the
  in-place `create_panel`/`clear_dashboard_panels` refresh** (batch it if
  needed); revisit only if an in-place apply lands.
- **Chart `annotation` is a footnote/subtitle, not an on-canvas marker.** Use it
  for provenance + event framing, not a plotted vertical line.
- **No uploads-delete tool.** `upload_image` for hero images would accumulate
  with no cleanup → **keep hotlinking outlet CDNs for now** (with the existing
  widest-variant logic); revisit if a delete endpoint appears.
- **Proposal-path enum drift.** The production (`dist`) MCP's proposal schema
  lists `divider` but not `collection`/`timeline`, though HEAD source includes
  them. Use `create_panel` for collection/timeline; verify before trusting the
  proposal path for them.

## Open questions

1. FRED vs. a keyless-only start (OWID/World Bank) to avoid a new secret for v1?
2. Series-map (safe) only for P2, or also ship the model-proposed-id path?
3. P3 source allowlist — which domains count as "authoritative"?
4. Is the ~20-min run budget elastic enough for P3, or must it be a separate,
   slower cadence?
