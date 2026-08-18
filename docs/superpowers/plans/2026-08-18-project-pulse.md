# Project Pulse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily "pulse" dashboard per tracked project (helio, concertino) inside the existing helio-news pipeline — velocity, cycle time, and backlog health computed by real helio data pipelines from raw Linear/git data, plus a local-model "what shipped" narrative.

**Architecture:** New `news/providers/linear.py` (direct GraphQL client — the interactive `mcp__linear__*` tools this plan's author used to verify queries do NOT exist in the unattended script) and `news/projects/` package (metrics, git log, narrative, orchestration) plug into the existing daily `news.run` job. Raw per-ticket data uploads to helio via `create_csv_data_source`; every statistic (averages, counts, week-bucketing, sorting/limiting) is computed by a helio pipeline (`time-series`/`single-row`/`top-n` shapes, or hand-rolled `filter`+`aggregate` steps), not Python — the one exception is `cycleTimeDays`/`ageDays`, precomputed per-row in Python because helio's `compute` step cannot do date arithmetic on CSV-sourced values (verified live, see spec).

**Tech Stack:** Python 3.14, `requests` (already a dependency), the existing `HelioClient`/MCP stdio wrapper, `gpt-oss` via the existing `Ollama` class in `news/agents.py`.

**Spec:** `docs/superpowers/specs/2026-08-18-project-pulse-design.md`

## Global Constraints

- Two projects at launch: `Helio` (Linear team "Helio Platform", repo `/home/matt/Development/helio`) and `Concertino` (Linear team "Concertino", repo `/home/matt/Development/concertino`). Config-driven — new projects are a `config/outlets.yaml` entry, never a code change.
- `lookback_days: 90` (velocity + cycle time), `narrative_days: 7` (what-shipped window), `backlog_top_n: 5` — all overridable in config, these are the defaults.
- All CSV data sources use the `news-proj-<slug>-src-<key>` naming pattern (matches the existing `cleanup_news_resources()` sweep pattern `name.startswith("news-") and "-src-" in name` — no changes needed to that method) so daily cleanup catches them automatically.
- Fail-soft per project: a Linear/git failure for one project logs a warning and skips only that project's board. Must never abort the news boards or the other project's board.
- No commit/PR-volume metric panel — git log feeds the narrative pass only.
- `LINEAR_API_KEY` is already set in `.env` (done prior to this plan).
- Every new pure-logic module gets real unit tests (TDD). `news/providers/linear.py` is the one exception — matches `fred.py`/`yahoo.py`'s existing convention of zero unit tests, a live smoke test only (Task 9).

---

## Task 1: HelioClient — CSV source, shape/steps pipelines, and panel binding

**Files:**
- Modify: `news/helio_client.py`
- Test: `tests/test_helio_client.py`

**Interfaces:**
- Produces: `HelioClient.create_csv_source(name: str, content: str) -> str` (returns source id), `HelioClient.build_shape_pipeline(source_id: str, prefix: str, key: str, shape_id: str, params: dict) -> str` (returns output DataType id), `HelioClient.build_steps_pipeline(source_id: str, prefix: str, key: str, steps: list[dict]) -> str` (returns output DataType id), `HelioClient.bind_new_panel(dashboard_id: str, title: str, panel_type: str, output_type_id: str, mapping: dict, *, config: dict | None = None, chart_type: str | None = None) -> str` (returns panel id).
- Consumes: existing `HelioClient.call()`, `HelioClient._apply_appearance()`, module-level `_type_name()` — all already in `news/helio_client.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_helio_client.py` (the `_StubCalls`/`_client` helpers already exist there from the 2026-08-18 cleanup fix — reuse them):

```python
def test_create_csv_source_returns_id():
    stub = _StubCalls(responses={"create_csv_data_source": {"id": "src-99"}})
    client = _client(stub)

    result = asyncio.run(client.create_csv_source("news-proj-helio-src-completed", "id,title\n1,A\n"))

    assert result == "src-99"
    assert stub.calls == [("create_csv_data_source",
                            {"name": "news-proj-helio-src-completed", "content": "id,title\n1,A\n"})]


def test_build_shape_pipeline_creates_runs_and_returns_output_type():
    stub = _StubCalls(responses={
        "create_pipeline_from_shape": {"id": "pipe-1"},
        "run_pipeline": {"outputDataTypeId": "type-1"},
    })
    client = _client(stub)

    result = asyncio.run(client.build_shape_pipeline(
        "src-99", "news-proj-helio", "velocity", "time-series",
        {"timeField": "completedAt", "granularity": "week",
         "measures": [{"fn": "count", "field": "id", "alias": "ticketsCompleted"}]}))

    assert result == "type-1"
    shape_call = next(c for t, c in stub.calls if t == "create_pipeline_from_shape")
    assert shape_call["shapeId"] == "time-series"
    assert shape_call["sourceDataSourceId"] == "src-99"
    assert shape_call["outputDataTypeName"] == "news_out_proj_helio_velocity"
    assert shape_call["params"]["timeField"] == "completedAt"
    run_call = next(c for t, c in stub.calls if t == "run_pipeline")
    assert run_call == {"pipelineId": "pipe-1"}


def test_build_steps_pipeline_creates_adds_steps_runs_and_returns_output_type():
    stub = _StubCalls(responses={
        "create_pipeline": {"id": "pipe-2"},
        "run_pipeline": {"outputDataTypeId": "type-2"},
    })
    client = _client(stub)
    steps = [
        {"type": "filter", "config": {"combinator": "AND",
                                       "conditions": [{"field": "isBug", "operator": "=", "value": "true"}]}},
        {"type": "aggregate", "config": {"groupBy": [],
                                          "aggregations": [{"alias": "openBugCount", "field": "id", "fn": "count"}]}},
    ]

    result = asyncio.run(client.build_steps_pipeline("src-100", "news-proj-helio", "openbugs", steps))

    assert result == "type-2"
    create_call = next(c for t, c in stub.calls if t == "create_pipeline")
    assert create_call["sourceDataSourceId"] == "src-100"
    assert create_call["outputDataTypeName"] == "news_out_proj_helio_openbugs"
    step_calls = [c for t, c in stub.calls if t == "add_pipeline_step"]
    assert len(step_calls) == 2
    assert step_calls[0] == {"pipelineId": "pipe-2", "type": "filter", "config": steps[0]["config"]}
    assert step_calls[1] == {"pipelineId": "pipe-2", "type": "aggregate", "config": steps[1]["config"]}
    run_call = next(c for t, c in stub.calls if t == "run_pipeline")
    assert run_call == {"pipelineId": "pipe-2"}


def test_bind_new_panel_metric_no_appearance_call():
    stub = _StubCalls(responses={"create_panel": {"id": "panel-1"}})
    client = _client(stub)

    result = asyncio.run(client.bind_new_panel(
        "dash-1", "Avg Cycle Time", "metric", "type-1", {"value": "avgCycleTimeDays"}))

    assert result == "panel-1"
    create_call = next(c for t, c in stub.calls if t == "create_panel")
    assert create_call == {"dashboardId": "dash-1", "type": "metric", "title": "Avg Cycle Time"}
    bind_call = next(c for t, c in stub.calls if t == "bind_panel")
    assert bind_call == {"panelId": "panel-1", "dataTypeId": "type-1",
                          "fieldMapping": {"value": "avgCycleTimeDays"}, "panelType": "metric"}
    assert not any(t == "update_panel_appearance" for t, _ in stub.calls)


def test_bind_new_panel_chart_applies_appearance():
    stub = _StubCalls(responses={"create_panel": {"id": "panel-2"}})
    client = _client(stub)

    asyncio.run(client.bind_new_panel(
        "dash-1", "Velocity", "chart", "type-2",
        {"xAxis": "completedAt", "yAxis": "ticketsCompleted"}, chart_type="bar"))

    appearance_call = next(c for t, c in stub.calls if t == "update_panel_appearance")
    assert appearance_call["panelId"] == "panel-2"
    assert appearance_call["appearance"]["chart"]["chartType"] == "bar"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_helio_client.py -v`
Expected: the 5 new tests FAIL with `AttributeError: 'HelioClient' object has no attribute 'create_csv_source'` (and similarly for the other 3 new methods) — the existing tests from the cleanup fix still PASS.

- [ ] **Step 3: Implement the four methods**

Add to `news/helio_client.py`, inside the `HelioClient` class, after `build_bound_panel`/`_apply_appearance` (around where `add_text_panel` currently starts):

```python
    async def create_csv_source(self, name: str, content: str) -> str:
        """Create a CSV data source from inline text. Returns the source id.
        Unlike build_bound_panel's inline `create_data_source` (JSON rows),
        this is for raw tabular data a helio pipeline will aggregate — see
        build_shape_pipeline/build_steps_pipeline."""
        source = await self.call("create_csv_data_source", {"name": name, "content": content})
        return source["id"]

    async def build_shape_pipeline(self, source_id: str, prefix: str, key: str,
                                   shape_id: str, params: dict) -> str:
        """Instantiate a smart pipeline shape (time-series/single-row/top-n/...)
        against an existing source, run it, return the output DataType id.
        Caller still does its own create_panel/bind_panel (see bind_new_panel) —
        this only builds and runs the pipeline, same division of labor as
        build_bound_panel's manual chain."""
        pipe = await self.call("create_pipeline_from_shape", {
            "shapeId": shape_id, "sourceDataSourceId": source_id,
            "outputDataTypeName": _type_name(prefix, key),
            "name": f"{prefix}-pipe-{key}", "params": params,
        })
        run = await self.call("run_pipeline", {"pipelineId": pipe["id"]})
        return run["outputDataTypeId"]

    async def build_steps_pipeline(self, source_id: str, prefix: str, key: str,
                                   steps: list[dict]) -> str:
        """Build a pipeline from hand-rolled steps, for shapes that don't fit
        (e.g. filter-then-aggregate — no shape combines the two). Mirrors
        build_bound_panel's step-adding loop. Returns the output DataType id."""
        pipe = await self.call("create_pipeline", {
            "name": f"{prefix}-pipe-{key}", "sourceDataSourceId": source_id,
            "outputDataTypeName": _type_name(prefix, key),
        })
        pipeline_id = pipe["id"]
        for step in steps:
            await self.call("add_pipeline_step", {
                "pipelineId": pipeline_id, "type": step["type"], "config": step["config"],
            })
        run = await self.call("run_pipeline", {"pipelineId": pipeline_id})
        return run["outputDataTypeId"]

    async def bind_new_panel(self, dashboard_id: str, title: str, panel_type: str,
                             output_type_id: str, mapping: dict, *,
                             config: dict | None = None, chart_type: str | None = None) -> str:
        """create_panel + bind_panel (+ appearance for a chart) against an
        already-run pipeline's output DataType. The tail half of
        build_bound_panel, for callers (build_shape_pipeline/
        build_steps_pipeline) that built their own pipeline instead of
        taking a SourceData. Returns the panel id."""
        create_args: dict = {"dashboardId": dashboard_id, "type": panel_type, "title": title}
        if config:
            create_args["config"] = config
        panel = await self.call("create_panel", create_args)
        panel_id = panel["id"]
        await self.call("bind_panel", {
            "panelId": panel_id, "dataTypeId": output_type_id,
            "fieldMapping": mapping, "panelType": panel_type,
        })
        if chart_type:
            await self._apply_appearance(panel_id, chart_type=chart_type)
        return panel_id
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_helio_client.py -v`
Expected: all tests PASS (8 existing + 5 new = 13).

- [ ] **Step 5: Commit**

```bash
git add news/helio_client.py tests/test_helio_client.py
git commit -m "feat: HelioClient support for CSV sources and shape/steps pipelines"
```

---

## Task 2: `news/projects/metrics.py` — pure per-row math and CSV construction

**Files:**
- Create: `news/projects/__init__.py` (empty)
- Create: `news/projects/metrics.py`
- Test: `tests/test_projects_metrics.py`

**Interfaces:**
- Produces: `cycle_time_days(started_at: str | None, completed_at: str | None) -> float | None`, `age_days(created_at: str | None, now: datetime) -> float | None`, `completed_csv(issues: list[dict]) -> str`, `open_csv(issues: list[dict], now: datetime) -> str`.
- Consumes: nothing outside the standard library. `issues` dicts have the shape Linear's GraphQL API returns (see Task 3): `{"identifier": str, "title": str, "priority": int, "createdAt": str, "startedAt": str | None, "completedAt": str | None, "labels": {"nodes": [{"name": str}, ...]}}`.

- [ ] **Step 1: Create the package init**

```bash
mkdir -p news/projects
touch news/projects/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_projects_metrics.py`:

```python
from datetime import datetime, timezone

from news.projects import metrics


# ── cycle_time_days ──────────────────────────────────────────────────────────

def test_cycle_time_days_computes_whole_and_fractional_days():
    result = metrics.cycle_time_days("2026-08-01T00:00:00.000Z", "2026-08-05T12:00:00.000Z")
    assert result == 4.5


def test_cycle_time_days_none_when_started_at_missing():
    assert metrics.cycle_time_days(None, "2026-08-05T00:00:00.000Z") is None


def test_cycle_time_days_none_when_completed_at_missing():
    assert metrics.cycle_time_days("2026-08-01T00:00:00.000Z", None) is None


# ── age_days ──────────────────────────────────────────────────────────────────

def test_age_days_computes_days_since_creation():
    now = datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc)
    result = metrics.age_days("2026-08-08T00:00:00.000Z", now)
    assert result == 10.0


def test_age_days_none_when_created_at_missing():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    assert metrics.age_days(None, now) is None


# ── completed_csv ─────────────────────────────────────────────────────────────

def test_completed_csv_header_and_rows():
    issues = [
        {"identifier": "HEL-1", "title": "Fix the thing", "startedAt": "2026-08-01T00:00:00.000Z",
         "completedAt": "2026-08-03T00:00:00.000Z"},
        {"identifier": "HEL-2", "title": "Ship the other thing", "startedAt": None,
         "completedAt": "2026-08-04T00:00:00.000Z"},
    ]
    csv_text = metrics.completed_csv(issues)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "id,title,completedAt,cycleTimeDays"
    assert lines[1] == "HEL-1,Fix the thing,2026-08-03T00:00:00.000Z,2.0"
    assert lines[2] == "HEL-2,Ship the other thing,2026-08-04T00:00:00.000Z,"


# ── open_csv ──────────────────────────────────────────────────────────────────

def test_open_csv_header_rows_and_bug_label_detection():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    issues = [
        {"identifier": "HEL-3", "title": "Old bug", "priority": 2,
         "createdAt": "2026-08-08T00:00:00.000Z", "labels": {"nodes": [{"name": "Bug"}]}},
        {"identifier": "HEL-4", "title": "Feature request", "priority": 3,
         "createdAt": "2026-08-16T00:00:00.000Z", "labels": {"nodes": [{"name": "Enhancement"}]}},
    ]
    csv_text = metrics.open_csv(issues, now)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "id,title,priority,isBug,createdAt,ageDays"
    assert lines[1] == "HEL-3,Old bug,2,true,2026-08-08T00:00:00.000Z,10.0"
    assert lines[2] == "HEL-4,Feature request,3,false,2026-08-16T00:00:00.000Z,2.0"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_projects_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'news.projects.metrics'`

- [ ] **Step 4: Implement `news/projects/metrics.py`**

```python
"""Pure per-row math for project-pulse CSVs. Everything a helio pipeline can
compute (averages, counts, week-bucketing, sorting) stays out of here — this
module exists only because helio's `compute` step cannot do date arithmetic
on CSV-sourced values (verified live; see
docs/superpowers/specs/2026-08-18-project-pulse-design.md). cycleTimeDays and
ageDays are the one per-row exception; every aggregate statistic downstream
is computed by a helio pipeline, not here."""

from __future__ import annotations

import csv
import io
from datetime import datetime


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def cycle_time_days(started_at: str | None, completed_at: str | None) -> float | None:
    started, completed = _parse(started_at), _parse(completed_at)
    if started is None or completed is None:
        return None
    return round((completed - started).total_seconds() / 86400, 2)


def age_days(created_at: str | None, now: datetime) -> float | None:
    created = _parse(created_at)
    if created is None:
        return None
    return round((now - created).total_seconds() / 86400, 2)


def completed_csv(issues: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "title", "completedAt", "cycleTimeDays"])
    for issue in issues:
        ct = cycle_time_days(issue.get("startedAt"), issue.get("completedAt"))
        writer.writerow([issue["identifier"], issue["title"], issue.get("completedAt", ""),
                         "" if ct is None else ct])
    return buf.getvalue()


def open_csv(issues: list[dict], now: datetime) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "title", "priority", "isBug", "createdAt", "ageDays"])
    for issue in issues:
        is_bug = any(label.get("name") == "Bug"
                     for label in issue.get("labels", {}).get("nodes", []))
        ad = age_days(issue.get("createdAt"), now)
        writer.writerow([issue["identifier"], issue["title"], issue.get("priority", 0),
                         "true" if is_bug else "false", issue.get("createdAt", ""),
                         "" if ad is None else ad])
    return buf.getvalue()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_projects_metrics.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add news/projects/__init__.py news/projects/metrics.py tests/test_projects_metrics.py
git commit -m "feat: project-pulse metrics module (cycle time, age, CSV construction)"
```

---

## Task 3: `news/projects/gitlog.py` — recent commit subjects

**Files:**
- Create: `news/projects/gitlog.py`
- Test: `tests/test_projects_gitlog.py`

**Interfaces:**
- Produces: `fetch_recent_subjects(repo_path: str, since_days: int) -> list[str]` — most-recent-first subject lines from `main`, empty list on any failure (bad path, not a git repo, `main` doesn't exist).
- Consumes: nothing beyond the standard library (`subprocess`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projects_gitlog.py`:

```python
import subprocess

from news.projects import gitlog


def _init_repo(tmp_path, subjects: list[str]):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    for i, subject in enumerate(subjects):
        (repo / f"file{i}.txt").write_text(subject)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", subject], cwd=repo, check=True)
    return repo


def test_fetch_recent_subjects_returns_most_recent_first(tmp_path):
    repo = _init_repo(tmp_path, ["HEL-1 First commit", "HEL-2 Second commit", "HEL-3 Third commit"])

    subjects = gitlog.fetch_recent_subjects(str(repo), since_days=30)

    assert subjects == ["HEL-3 Third commit", "HEL-2 Second commit", "HEL-1 First commit"]


def test_fetch_recent_subjects_returns_empty_list_for_nonexistent_path(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert gitlog.fetch_recent_subjects(str(missing), since_days=30) == []


def test_fetch_recent_subjects_returns_empty_list_for_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    assert gitlog.fetch_recent_subjects(str(not_a_repo), since_days=30) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_projects_gitlog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'news.projects.gitlog'`

- [ ] **Step 3: Implement `news/projects/gitlog.py`**

```python
"""Recent commit subjects from a local repo's `main` branch — raw material
for the project-pulse narrative pass (news/projects/narrative.py). Not a
metrics source; commit volume is deliberately not a tracked panel (see the
design spec's non-goals)."""

from __future__ import annotations

import subprocess


def fetch_recent_subjects(repo_path: str, since_days: int) -> list[str]:
    """Subject lines from `main`, most-recent-first, committed within the
    last `since_days`. Empty list on any failure (bad path, not a repo, no
    `main` branch) — this is best-effort narrative fuel, never fatal."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "main", f"--since={since_days}.days.ago",
             "--pretty=format:%s"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_projects_gitlog.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add news/projects/gitlog.py tests/test_projects_gitlog.py
git commit -m "feat: project-pulse git log fetcher"
```

---

## Task 4: `news/providers/linear.py` — GraphQL client

**Files:**
- Create: `news/providers/linear.py`

**Interfaces:**
- Produces: `fetch_completed(team_name: str, lookback_days: int) -> list[dict] | None`, `fetch_open(team_name: str) -> list[dict] | None`. Both return `None` only when `LINEAR_API_KEY` is unset (logged once); any HTTP/GraphQL failure raises (caught by the per-project try/except in Task 7's orchestration, matching the spec's fail-soft-per-project design, not fail-soft-inside-the-client — `fred.py`/`yahoo.py` follow the identical convention: `resp.raise_for_status()` uncaught).
- Consumes: `requests` (already a dependency — see `news/providers/fred.py`).

No unit test for this task — per the approved spec's testing section, this matches `fred.py`/`yahoo.py`'s existing convention (zero unit tests, live-verified only). The GraphQL queries below were verified live against the real Linear API during design (both teams, both completed and open-ticket variants, plus the `DateTimeOrDuration` lookback filter) — Task 9 re-verifies end-to-end as part of the full pipeline's live smoke test.

- [ ] **Step 1: Implement `news/providers/linear.py`**

```python
"""Linear GraphQL client for project-pulse — LINEAR_API_KEY-gated, fail-soft
on a missing key (matches fred.py/yahoo.py). This is a DIRECT HTTP client,
not the mcp__linear__* tools: those only exist inside an interactive Claude
session with the Linear MCP server configured, not for this unattended daily
script (news.run, launched by systemd with no MCP/Claude involved).

Unlike fred.py/yahoo.py, network/GraphQL failures here are NOT swallowed —
they propagate so news/projects/build.py's per-project try/except can log
and skip just that project, per the design spec's fail-soft-per-project
(not fail-soft-inside-the-client) philosophy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

_API = "https://api.linear.app/graphql"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_warned = False

_COMPLETED_QUERY = """
query($teamName: String!, $since: DateTimeOrDuration!) {
  issues(
    filter: { team: { name: { eq: $teamName } }, completedAt: { gte: $since } }
    first: 250
    orderBy: updatedAt
  ) {
    nodes {
      identifier
      title
      priority
      createdAt
      startedAt
      completedAt
      labels { nodes { name } }
    }
  }
}
"""

_OPEN_QUERY = """
query($teamName: String!) {
  issues(
    filter: { team: { name: { eq: $teamName } }, completedAt: { null: true }, canceledAt: { null: true } }
    first: 250
    orderBy: createdAt
  ) {
    nodes {
      identifier
      title
      priority
      createdAt
      labels { nodes { name } }
    }
  }
}
"""


def _api_key() -> str | None:
    """LINEAR_API_KEY from the environment, falling back to ./.env (gitignored)."""
    key = os.environ.get("LINEAR_API_KEY")
    if key:
        return key.strip()
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("LINEAR_API_KEY=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _query(query: str, variables: dict) -> list[dict] | None:
    global _warned
    key = _api_key()
    if not key:
        if not _warned:
            print("· LINEAR_API_KEY not set — project boards skipped (add it to .env "
                  "to enable them)", file=sys.stderr)
            _warned = True
        return None
    resp = requests.post(_API, json={"query": query, "variables": variables},
                         headers={"Authorization": key, "Content-Type": "application/json"},
                         timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear API error: {data['errors']}")
    return data["data"]["issues"]["nodes"]


def fetch_completed(team_name: str, lookback_days: int) -> list[dict] | None:
    """Tickets completed within the last `lookback_days` for `team_name` —
    for velocity + cycle-time metrics."""
    return _query(_COMPLETED_QUERY, {"teamName": team_name, "since": f"-P{lookback_days}D"})


def fetch_open(team_name: str) -> list[dict] | None:
    """Every currently-open (not completed, not canceled) ticket for
    `team_name`, no date bound — a stale old bug must still surface as
    "oldest open" even if untouched in months."""
    return _query(_OPEN_QUERY, {"teamName": team_name})
```

- [ ] **Step 2: Verify with a one-off live check**

Run this from the repo root (LINEAR_API_KEY is already in `.env`):

```bash
.venv/bin/python -c "
from news.providers import linear
completed = linear.fetch_completed('Helio Platform', 90)
open_ = linear.fetch_open('Concertino')
print('completed:', len(completed), 'tickets, sample:', completed[0]['identifier'] if completed else None)
print('open:', len(open_), 'tickets, sample:', open_[0]['identifier'] if open_ else None)
"
```

Expected: two counts printed, no traceback, both samples look like real ticket identifiers (`HEL-...`, `CON-...`).

- [ ] **Step 3: Commit**

```bash
git add news/providers/linear.py
git commit -m "feat: Linear GraphQL client for project-pulse"
```

---

## Task 5: `news/projects/narrative.py` — "what shipped" LLM pass

**Files:**
- Create: `news/projects/narrative.py`
- Test: `tests/test_projects_narrative.py`

**Interfaces:**
- Produces: `project_summary_pass(ollama, model: str, project_name: str, completed_titles: list[str], commit_subjects: list[str], think: str | None = None) -> str`.
- Consumes: `news.agents.Ollama` (specifically its `chat_json(model, system, user, temperature=0.2, think=None) -> dict` method — already exists, see `news/agents.py:79`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projects_narrative.py`:

```python
from unittest.mock import MagicMock

from news.projects import narrative


def test_project_summary_pass_returns_summary_from_ollama_response():
    ollama = MagicMock()
    ollama.chat_json.return_value = {"summary": "Shipped the pipeline detail redesign and MFA."}

    result = narrative.project_summary_pass(
        ollama, "gpt-oss:latest", "Helio",
        completed_titles=["Redesign pipeline detail page", "Add TOTP-based MFA"],
        commit_subjects=["HEL-719 Redesign the pipeline detail page chrome",
                         "HEL-702 Add TOTP-based MFA"],
        think="medium")

    assert result == "Shipped the pipeline detail redesign and MFA."
    ollama.chat_json.assert_called_once()
    call = ollama.chat_json.call_args
    assert call.args[0] == "gpt-oss:latest"  # model
    assert "Redesign pipeline detail page" in call.args[2]  # user payload
    assert call.kwargs.get("think") == "medium"


def test_project_summary_pass_returns_empty_string_on_empty_response():
    ollama = MagicMock()
    ollama.chat_json.return_value = {}

    result = narrative.project_summary_pass(
        ollama, "gpt-oss:latest", "Helio", completed_titles=[], commit_subjects=[])

    assert result == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_projects_narrative.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'news.projects.narrative'`

- [ ] **Step 3: Implement `news/projects/narrative.py`**

```python
"""One gpt-oss pass: turn a project's recently-completed ticket titles and
git commit subjects into a short "what shipped" paragraph. Structurally the
narrative counterpart to the metrics panels — those are code-computed ground
truth (see metrics.py); this is the one place a model's read on the period
appears, same division of labor as the news pipeline's summarizer pass vs.
its code-computed history/trend numbers."""

from __future__ import annotations

import json

_SYSTEM = (
    "You write a short, specific 'what shipped' summary for one software "
    "project's recent activity, for a developer checking in on their own "
    "project. You're given completed ticket titles and recent git commit "
    "subjects (which often restate ticket work in more technical terms). "
    "Write 2-4 sentences, prose, no bullet points, no headers. Name specific "
    "features/fixes, not vague summaries like 'various improvements'. If "
    "there's nothing to report, say so plainly in one short sentence — "
    "never pad or invent activity.\n"
    'Return ONLY JSON: {"summary": "..."}'
)


def project_summary_pass(ollama, model: str, project_name: str,
                         completed_titles: list[str], commit_subjects: list[str],
                         think: str | None = None) -> str:
    user = json.dumps({
        "project": project_name,
        "completed_tickets": completed_titles,
        "recent_commits": commit_subjects,
    })
    result = ollama.chat_json(model, _SYSTEM, user, think=think)
    return result.get("summary", "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_projects_narrative.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add news/projects/narrative.py tests/test_projects_narrative.py
git commit -m "feat: project-pulse 'what shipped' narrative pass"
```

---

## Task 6: Config — `projects:` block + model/reasoning keys

**Files:**
- Modify: `config/outlets.yaml`

**Interfaces:**
- Produces: `config["projects"]` (`enabled`, `lookback_days`, `narrative_days`, `backlog_top_n`, `items: [{name, linear_team, repo_path}]`), `config["models"]["projects_summary"]`, `config["reasoning"]["projects_summary"]` — all read by Task 7's `news/projects/build.py`.
- Consumes: nothing (config is loaded verbatim by `news.fetch.load_config`, which is plain `yaml.safe_load` — no schema to update).

- [ ] **Step 1: Add the `projects:` block**

Open `config/outlets.yaml`. The `dashboards:` block ends at line 88 (`    "Markets & Business": ["markets", "business"]`), followed by a blank line and a comment block leading into `stocks:` at line 93. Insert the new block between the blank line and that comment block (i.e. right after line 88's `"Markets & Business"` entry):

```yaml
projects:
  enabled: true
  lookback_days: 90     # velocity + cycle-time window
  narrative_days: 7     # "what shipped" rolling window — kept short and
                         # separate from lookback_days so a quiet week
                         # doesn't produce an empty narrative panel
  backlog_top_n: 5
  items:
    - name: "Helio"
      linear_team: "Helio Platform"
      repo_path: "/home/matt/Development/helio"
    - name: "Concertino"
      linear_team: "Concertino"
      repo_path: "/home/matt/Development/concertino"
```

- [ ] **Step 2: Add the model + reasoning keys**

In the `models:` block, the last entry is `verifier: "gpt-oss:latest"   # adversarially audits...` (currently the final line before the blank line and reasoning-effort comment). Add immediately after it:

```yaml
  projects_summary: "gpt-oss:latest"  # "what shipped" narrative pass
```

In the `reasoning:` block, the last entry is `verifier: high              # adversarial audit...`. Add immediately after it:

```yaml
  projects_summary: medium  # mechanical summarization, not judgment-heavy clustering
```

- [ ] **Step 3: Verify the config loads and parses correctly**

Run:

```bash
.venv/bin/python -c "
from news.fetch import load_config
c = load_config()
p = c['projects']
assert p['enabled'] is True
assert p['lookback_days'] == 90
assert p['narrative_days'] == 7
assert p['backlog_top_n'] == 5
assert p['items'][0] == {'name': 'Helio', 'linear_team': 'Helio Platform', 'repo_path': '/home/matt/Development/helio'}
assert p['items'][1] == {'name': 'Concertino', 'linear_team': 'Concertino', 'repo_path': '/home/matt/Development/concertino'}
assert c['models']['projects_summary'] == 'gpt-oss:latest'
assert c['reasoning']['projects_summary'] == 'medium'
print('OK')
"
```

Expected: prints `OK`, no assertion error.

- [ ] **Step 4: Commit**

```bash
git add config/outlets.yaml
git commit -m "feat: add projects config block for project-pulse dashboards"
```

---

## Task 7: `news/projects/build.py` — per-project orchestration

**Files:**
- Create: `news/projects/build.py`
- Test: `tests/test_projects_build.py`

**Interfaces:**
- Produces: `async def build_project_boards(config: dict, helio: HelioClient, board_ids: dict[str, str]) -> None` — the entry point Task 8 wires into `news/run.py`.
- Consumes: `news.helio_client.HelioClient` (`.create_csv_source`, `.build_shape_pipeline`, `.build_steps_pipeline`, `.bind_new_panel`, `.add_text_panel` — all already exist), `news.providers.linear` (`.fetch_completed`, `.fetch_open`), `news.projects.metrics` (`.completed_csv`, `.open_csv`), `news.projects.gitlog` (`.fetch_recent_subjects`), `news.projects.narrative` (`.project_summary_pass`), `news.agents.Ollama`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projects_build.py`. This stubs every collaborator (Linear, git log, HelioClient, Ollama) so the test exercises only `build.py`'s own orchestration logic — the collaborators each have their own tests already (Tasks 1-5).

```python
import asyncio
from unittest.mock import patch

from news.projects import build


def _config():
    return {
        "projects": {
            "enabled": True, "lookback_days": 90, "narrative_days": 7, "backlog_top_n": 5,
            "items": [
                {"name": "Helio", "linear_team": "Helio Platform", "repo_path": "/repo/helio"},
                {"name": "Concertino", "linear_team": "Concertino", "repo_path": "/repo/concertino"},
            ],
        },
        "models": {"projects_summary": "gpt-oss:latest"},
        "reasoning": {"projects_summary": "medium"},
        "ollama": {},
    }


class _FakeHelio:
    def __init__(self):
        self.csv_sources_created = []
        self.shape_pipelines_built = []
        self.steps_pipelines_built = []
        self.panels_bound = []
        self.text_panels_added = []

    async def create_csv_source(self, name, content):
        self.csv_sources_created.append((name, content))
        return f"src-{len(self.csv_sources_created)}"

    async def build_shape_pipeline(self, source_id, prefix, key, shape_id, params):
        self.shape_pipelines_built.append((source_id, prefix, key, shape_id, params))
        return f"type-shape-{len(self.shape_pipelines_built)}"

    async def build_steps_pipeline(self, source_id, prefix, key, steps):
        self.steps_pipelines_built.append((source_id, prefix, key, steps))
        return f"type-steps-{len(self.steps_pipelines_built)}"

    async def bind_new_panel(self, dashboard_id, title, panel_type, output_type_id, mapping,
                             *, config=None, chart_type=None):
        self.panels_bound.append((dashboard_id, title, panel_type, output_type_id, mapping, chart_type))
        return f"panel-{len(self.panels_bound)}"

    async def add_text_panel(self, dashboard_id, title, content, markdown=True):
        self.text_panels_added.append((dashboard_id, title, content))
        return f"panel-text-{len(self.text_panels_added)}"


_COMPLETED = [{"identifier": "HEL-1", "title": "Ship the thing", "priority": 2,
              "createdAt": "2026-08-01T00:00:00.000Z", "startedAt": "2026-08-01T00:00:00.000Z",
              "completedAt": "2026-08-03T00:00:00.000Z", "labels": {"nodes": []}}]
_OPEN = [{"identifier": "HEL-2", "title": "Old bug", "priority": 1,
         "createdAt": "2026-08-01T00:00:00.000Z", "labels": {"nodes": [{"name": "Bug"}]}}]


def test_build_project_boards_builds_five_panels_per_project():
    helio = _FakeHelio()
    board_ids = {"Helio": "dash-helio", "Concertino": "dash-concertino"}

    with patch("news.projects.build.linear.fetch_completed", return_value=_COMPLETED), \
         patch("news.projects.build.linear.fetch_open", return_value=_OPEN), \
         patch("news.projects.build.gitlog.fetch_recent_subjects", return_value=["HEL-1 Ship the thing"]), \
         patch("news.projects.build.narrative.project_summary_pass", return_value="Shipped one thing."):
        asyncio.run(build.build_project_boards(_config(), helio, board_ids))

    # 2 CSV sources (completed + open) per project x 2 projects
    assert len(helio.csv_sources_created) == 4
    # velocity (shape) + avg cycle time (shape) + oldest open (shape) = 3 shape pipelines per project
    assert len(helio.shape_pipelines_built) == 6
    # open bug count = 1 steps pipeline per project
    assert len(helio.steps_pipelines_built) == 2
    # 4 pipeline-bound panels (velocity, avg cycle time, open bugs, oldest open)
    # + 1 markdown narrative panel = 5 per project, x2 projects
    assert len(helio.panels_bound) == 8
    assert len(helio.text_panels_added) == 2


def test_build_project_boards_skips_disabled():
    helio = _FakeHelio()
    config = _config()
    config["projects"]["enabled"] = False

    asyncio.run(build.build_project_boards(config, helio, {"Helio": "dash-helio"}))

    assert helio.csv_sources_created == []


def test_build_project_boards_skips_one_project_on_fetch_failure_not_the_other():
    helio = _FakeHelio()
    board_ids = {"Helio": "dash-helio", "Concertino": "dash-concertino"}

    def fetch_completed(team, lookback):
        if team == "Helio Platform":
            raise RuntimeError("Linear API error: boom")
        return _COMPLETED

    with patch("news.projects.build.linear.fetch_completed", side_effect=fetch_completed), \
         patch("news.projects.build.linear.fetch_open", return_value=_OPEN), \
         patch("news.projects.build.gitlog.fetch_recent_subjects", return_value=[]), \
         patch("news.projects.build.narrative.project_summary_pass", return_value=""):
        asyncio.run(build.build_project_boards(_config(), helio, board_ids))

    # Concertino (the second project) still built its 5 panels despite Helio's failure
    assert len(helio.text_panels_added) == 1
    assert helio.text_panels_added[0][0] == "dash-concertino"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_projects_build.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'news.projects.build'`

- [ ] **Step 3: Implement `news/projects/build.py`**

```python
"""Per-project pulse boards: fetch Linear + git activity, upload raw CSVs,
let helio's pipelines compute every statistic, add one LLM narrative panel.
Called from news/run.py's apply_plan, inside the same HelioClient session
the news boards use — see the design spec for why this shape (not the news
pipeline's triage/extract/critic/planner chain)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from ..agents import Ollama
from ..helio_client import HelioClient
from ..providers import linear
from . import gitlog, metrics, narrative


def _slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


async def build_project_boards(config: dict, helio: HelioClient, board_ids: dict[str, str]) -> None:
    """board_ids maps project name -> an already-ensured, already-cleared
    dashboard id (news/run.py's apply_plan folds project names into its
    existing board_ids setup so cleanup_news_resources() catches
    project-pulse resources in the same sweep as the news boards)."""
    pcfg = config.get("projects", {})
    if not pcfg.get("enabled"):
        return
    for item in pcfg.get("items", []):
        dashboard_id = board_ids.get(item["name"])
        if dashboard_id is None:
            continue
        try:
            await _build_one_project(config, helio, item, dashboard_id)
            print(f"· project '{item['name']}' board refreshed", file=sys.stderr)
        except Exception as e:
            print(f"· project '{item['name']}' skipped: {e}", file=sys.stderr)


async def _build_one_project(config: dict, helio: HelioClient, item: dict, dashboard_id: str) -> None:
    pcfg = config.get("projects", {})
    lookback_days = int(pcfg.get("lookback_days", 90))
    narrative_days = int(pcfg.get("narrative_days", 7))
    top_n = int(pcfg.get("backlog_top_n", 5))
    team = item["linear_team"]
    repo_path = item["repo_path"]
    prefix = f"news-proj-{_slug(item['name'])}"

    completed = linear.fetch_completed(team, lookback_days)
    open_tickets = linear.fetch_open(team)
    if completed is None or open_tickets is None:
        raise RuntimeError("LINEAR_API_KEY not set")

    now = datetime.now(timezone.utc)
    completed_src = await helio.create_csv_source(
        f"{prefix}-src-completed", metrics.completed_csv(completed))
    open_src = await helio.create_csv_source(
        f"{prefix}-src-open", metrics.open_csv(open_tickets, now))

    # velocity trend
    velocity_type = await helio.build_shape_pipeline(
        completed_src, prefix, "velocity", "time-series",
        {"timeField": "completedAt", "granularity": "week",
         "measures": [{"fn": "count", "field": "id", "alias": "ticketsCompleted"}]})
    await helio.bind_new_panel(
        dashboard_id, "Velocity", "chart", velocity_type,
        {"xAxis": "completedAt", "yAxis": "ticketsCompleted"}, chart_type="bar")

    # avg cycle time
    cycle_type = await helio.build_shape_pipeline(
        completed_src, prefix, "cycletime", "single-row",
        {"mode": "aggregate",
         "measures": [{"fn": "avg", "field": "cycleTimeDays", "alias": "avgCycleTimeDays"}]})
    await helio.bind_new_panel(
        dashboard_id, "Avg Cycle Time (days)", "metric", cycle_type, {"value": "avgCycleTimeDays"})

    # open bug count — filter then aggregate; no shape combines the two
    bug_type = await helio.build_steps_pipeline(
        open_src, prefix, "openbugs",
        [{"type": "filter", "config": {"combinator": "AND",
                                        "conditions": [{"field": "isBug", "operator": "=", "value": "true"}]}},
         {"type": "aggregate", "config": {"groupBy": [],
                                           "aggregations": [{"alias": "openBugCount", "field": "id", "fn": "count"}]}}])
    await helio.bind_new_panel(
        dashboard_id, "Open Bugs", "metric", bug_type, {"value": "openBugCount"})

    # oldest open tickets
    oldest_type = await helio.build_shape_pipeline(
        open_src, prefix, "oldest", "top-n",
        {"measure": "ageDays", "direction": "desc", "n": top_n})
    await helio.bind_new_panel(
        dashboard_id, "Oldest Open Tickets", "table", oldest_type, {"columns": "title,ageDays"})

    # narrative
    commits = gitlog.fetch_recent_subjects(repo_path, narrative_days)
    narrative_titles = [
        c["title"] for c in completed
        if metrics.age_days(c.get("completedAt"), now) is not None
        and metrics.age_days(c.get("completedAt"), now) <= narrative_days
    ]
    oc = config.get("ollama", {})
    models = config.get("models", {})
    effort = config.get("reasoning", {})
    ollama = Ollama(oc.get("host", "http://localhost:11434"),
                    oc.get("timeout_seconds", 180), oc.get("num_ctx"))
    summary = narrative.project_summary_pass(
        ollama, models.get("projects_summary", "gpt-oss:latest"), item["name"],
        narrative_titles, commits, effort.get("projects_summary"))
    await helio.add_text_panel(
        dashboard_id, "What Shipped",
        summary or "_Quiet period — nothing shipped._")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_projects_build.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add news/projects/build.py tests/test_projects_build.py
git commit -m "feat: project-pulse per-project board orchestration"
```

---

## Task 8: Wire into `news/run.py`

**Files:**
- Modify: `news/run.py:327-410` (the `apply_plan` function)
- Test: `tests/test_run_history.py` (extend — it already patches `apply_plan`-adjacent pieces of `run.py`)

**Interfaces:**
- Consumes: `news.projects.build.build_project_boards(config, helio, board_ids) -> None` (Task 7).
- Produces: nothing new for other tasks — this is the final integration point.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_history.py`:

```python
def test_apply_plan_folds_project_names_into_board_ids_and_calls_build_project_boards():
    import asyncio
    from datetime import date

    config = {
        "dashboards": {"overview": "News Overview", "sections": {"Tech & AI": ["tech", "ai"]}},
        "projects": {"enabled": True, "items": [{"name": "Helio", "linear_team": "Helio Platform",
                                                  "repo_path": "/repo/helio"}]},
    }
    fake_plan = SimpleNamespace(day=date(2026, 8, 18), stories=[],
                                resource_prefix=lambda: "news")

    class _FakeHelio:
        def __init__(self):
            self.ensured = []
            self.cleared = []

        async def tool_names(self):
            return {"delete_data_source", "delete_dashboard", "delete_data_type", "delete_panel"}

        async def ensure_dashboard(self, name):
            self.ensured.append(name)
            return f"dash-{name}"

        async def clear_dashboard_panels(self, dashboard_id):
            self.cleared.append(dashboard_id)
            return 0

        async def cleanup_news_resources(self):
            return {"sources": 0, "types": 0}

    fake_helio = _FakeHelio()

    class _FakeSession:
        async def __aenter__(self):
            return fake_helio

        async def __aexit__(self, *exc):
            return False

    # apply_plan's day-in-review loop calls briefing.recap(plan, config) even
    # with zero stories/articles, and recap() reads real state/history/*.json
    # files from disk — patch it out so this test doesn't depend on (or
    # break on changes to) this repo's real history data, and doesn't need
    # _FakeHelio to implement build_bound_panel/set_layout for a panel this
    # test isn't about.
    with patch.object(run.HelioClient, "session", return_value=_FakeSession()), \
         patch("news.run.briefing.recap", return_value=None), \
         patch("news.run.build_project_boards") as build_mock:
        asyncio.run(run.apply_plan(fake_plan, [], config, {}, cleanup=True))

    assert "Helio" in fake_helio.ensured
    assert "dash-Helio" in fake_helio.cleared
    build_mock.assert_called_once()
    call_args = build_mock.call_args.args
    assert call_args[0] is config
    assert call_args[1] is fake_helio
    assert call_args[2]["Helio"] == "dash-Helio"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_run_history.py::test_apply_plan_folds_project_names_into_board_ids_and_calls_build_project_boards -v`
Expected: FAIL — either `AttributeError` (no `build_project_boards` imported into `news.run`) or the assertions on `fake_helio.ensured`/`build_mock` fail because `apply_plan` doesn't yet know about `projects`.

- [ ] **Step 3: Wire it in**

In `news/run.py`, add the import near the top (alongside the other `from .` imports, e.g. after `from .helio_client import HelioClient`):

```python
from .projects.build import build_project_boards
```

Then modify `apply_plan` (currently `news/run.py:327-348`):

```python
async def apply_plan(plan: DayPlan, articles: list, config: dict, curation: dict,
                     cleanup: bool = True) -> None:
    """Build the day across an overview board plus the configured section boards
    plus one board per configured project (news/projects/build.py)."""
    dcfg = config.get("dashboards", {})
    overview_name = dcfg.get("overview", "News Overview")
    overview_size = int(dcfg.get("overview_size", 5))
    section_names = list(dcfg.get("sections", {}).keys())
    pcfg = config.get("projects", {})
    project_names = [p["name"] for p in pcfg.get("items", [])] if pcfg.get("enabled") else []
    all_boards = [overview_name] + section_names + project_names
    routing = _domain_to_board(config)
    colors = config.get("sentiment", {}).get("colors", {})

    async with HelioClient.session(config) as helio:
        missing = REQUIRED_DELETE_TOOLS - await helio.tool_names()
        if missing:
            raise RuntimeError(
                f"helio MCP server is missing delete tools {missing}. Rebuild it "
                f"(helio-mcp: npm run build) so daily cleanup works."
            )

        board_ids = {name: await helio.ensure_dashboard(name) for name in all_boards}
        for name, did in board_ids.items():
            print(f"· board '{name}' → {did}", file=sys.stderr)

        # One cleanup for the whole run: clear every board (news AND project),
        # then delete the shared news-* sources/types once (they're not
        # board-scoped) — project-pulse CSV sources use the same news-*-src-*
        # naming (see news/projects/build.py's `prefix`), so this same sweep
        # catches their leftovers too, no separate cleanup logic needed.
        if cleanup:
            cleared = 0
            for did in board_ids.values():
                cleared += await helio.clear_dashboard_panels(did)
            gone = await helio.cleanup_news_resources()
            print(f"· cleanup: {cleared} panels, {gone['sources']} sources, "
                  f"{gone['types']} types removed", file=sys.stderr)
```

Leave everything from `prefix = plan.resource_prefix()` through the end of the function unchanged, EXCEPT add one line at the very end of `apply_plan` (`news/run.py:408-409` today):

```python
        print(f"· '{overview_name}': {len(built)} panels / {len(picks)} headliners",
              file=sys.stderr)
        await build_project_boards(config, helio, board_ids)
```

The new line is the last statement in the function, still inside the `async with HelioClient.session(config) as helio:` block (same indentation as the `print(...)` call above it).

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_run_history.py -v`
Expected: all tests PASS, including the new one.

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests PASS (the full suite — Tasks 1-7's tests plus every pre-existing test).

- [ ] **Step 6: Commit**

```bash
git add news/run.py tests/test_run_history.py
git commit -m "feat: wire project-pulse boards into the daily apply_plan run"
```

---

## Task 9: Live smoke test

**Files:** none (verification only — no code changes)

- [ ] **Step 1: Run a real plan-only pass to make sure nothing upstream broke**

```bash
.venv/bin/python -m news.run --plan-only
```

Expected: completes normally, prints the usual fetch/gemma timing output — `projects` isn't invoked in `--plan-only` mode (it's inside `apply_plan`, which `--plan-only` skips entirely, same as the news boards).

- [ ] **Step 2: Run a real full build**

```bash
.venv/bin/python -m news.run
```

Expected: completes with `✅ dashboards refreshed.` and, among the existing `· board '...' → ...` lines, two new ones for `Helio` and `Concertino`. Watch stderr for `· project 'Helio' board refreshed` and `· project 'Concertino' board refreshed` (or a `· project '...' skipped: ...` line — if that appears, treat it as a bug to investigate, not an expected outcome, since both Linear teams and both repo paths are confirmed to exist).

- [ ] **Step 3: Verify the two boards in helio**

Use the same live-probe pattern as the rest of this project (spawn `node dist/index.js` over stdio, `list_dashboards`, `get_dashboard`) to confirm:
- `Helio` and `Concertino` dashboards exist with 5 panels each (What Shipped, Velocity, Avg Cycle Time, Open Bugs, Oldest Open Tickets).
- The Velocity chart panel and Avg Cycle Time / Open Bugs metric panels are bound (not empty) — `get_data_type_rows` on each panel's `dataTypeId` returns at least one row.
- The What Shipped panel's markdown content is a real paragraph, not the `_Quiet period — nothing shipped._` fallback (unless the project genuinely had zero completed tickets in the last 7 days, which is unlikely for either team based on today's data).

- [ ] **Step 4: Confirm no residue if anything needed a throwaway check**

If any additional manual probing happened during this step, verify zero residue the same way Task-adjacent work earlier in this project did: `get_workspace_context`, check for any stray `probe-*`/`probe_out_*` named sources/types, delete any found.
