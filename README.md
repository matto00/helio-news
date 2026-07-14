# helio-news

A local, personal news aggregator that builds **content-shaped ("alive")**
dashboards in [helio](../helio) every morning. Feeds are pulled as RSS, a
sequence of local **gemma** passes (via ollama) clusters and interprets them and
*decides which panels each story needs*, and the result is written to helio
entirely through the **helio MCP server** (Python is the MCP client — auth stays
in the server, no REST calls here).

> An IPO story gets a stock chart + KPI + headlines. A Padres playoff story gets
> headlines (+ rosters/stats once a sports enricher is added). A pure political
> story just gets headlines + a summary. The planner pass chooses per story.

## Pipeline

```
RSS (feedparser) ─► gemma sequence (ollama, sequential) ─► enrichers ─► helio (MCP client)
  config/outlets    1 triage    cluster + domain + rank      stock:  yfinance    create dated
                    2 planner   choose panels + data keys     headlines: articles  sources+pipelines,
                    3 summarize  headline + summary                                build/refresh dashboard,
                                                                                   delete prior run
```

Each gemma pass is a separate ollama call with its own system prompt and narrow
input — far more reliable on a 4B model than one mega-prompt, and they run
**strictly sequentially** so only one model is resident at a time (16 GB GPU).

## Layout

| Path | Role |
|------|------|
| `config/outlets.yaml` | feeds, watchlist/tickers, model-per-pass, helio settings |
| `news/fetch.py` | RSS ingestion + `--check` feed validator |
| `news/agents.py` | the gemma sequence (triage → planner → summarizer) |
| `news/plan_schema.py` | the planner contract; validates/repairs gemma output |
| `news/enrichers/` | pluggable aux-data: `stocks.py` (yfinance), `headlines.py`; add `sports.py` etc. |
| `news/helio_client.py` | MCP client wrapper (spawns the helio MCP server over stdio) |
| `news/run.py` | daily driver (`--plan-only`, `--keep`) |
| `deploy/` | systemd user service + timer |

## Setup

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# helio auth for the MCP server Python spawns (gitignored; never in config/code):
cat > .env <<'EOF'
HELIO_PAT=helio_pat_...
HELIO_API_BASE_URL=https://helio-backend-...run.app
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
- **New panel kind** (e.g. sports rosters): add `news/enrichers/sports.py`
  exposing `build(arg, panel, story) -> SourceData`, register its prefix in
  `enrichers/REGISTRY`, and add the `data` key to the planner prompt in
  `agents.py`. Nothing else changes — an unknown/failed enricher just drops that
  panel, leaving the story's headlines fallback.
- **Route a pass to a bigger model:** change one line under `models:` in the config.
