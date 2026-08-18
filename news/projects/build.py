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
    velocity_panel_id = await helio.bind_new_panel(
        dashboard_id, "Velocity", "chart", velocity_type,
        {"xAxis": "completedAt", "yAxis": "ticketsCompleted"}, chart_type="bar")

    # avg cycle time
    cycle_type = await helio.build_shape_pipeline(
        completed_src, prefix, "cycletime", "single-row",
        {"mode": "aggregate",
         "measures": [{"fn": "avg", "field": "cycleTimeDays", "alias": "avgCycleTimeDays"}]})
    cycle_time_panel_id = await helio.bind_new_panel(
        dashboard_id, "Avg Cycle Time (days)", "metric", cycle_type, {"value": "avgCycleTimeDays"})

    # open bug count — filter then aggregate; no shape combines the two
    bug_type = await helio.build_steps_pipeline(
        open_src, prefix, "openbugs",
        [{"type": "filter", "config": {"combinator": "AND",
                                        "conditions": [{"field": "isBug", "operator": "=", "value": "true"}]}},
         {"type": "aggregate", "config": {"groupBy": [],
                                           "aggregations": [{"alias": "openBugCount", "field": "id", "fn": "count"}]}}])
    open_bugs_panel_id = await helio.bind_new_panel(
        dashboard_id, "Open Bugs", "metric", bug_type, {"value": "openBugCount"})

    # oldest open tickets
    oldest_type = await helio.build_shape_pipeline(
        open_src, prefix, "oldest", "top-n",
        {"measure": "ageDays", "direction": "desc", "n": top_n})
    oldest_panel_id = await helio.bind_new_panel(
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
    narrative_panel_id = await helio.add_text_panel(
        dashboard_id, "What Shipped",
        summary or "_Quiet period — nothing shipped._")

    # Fixed layout — project-pulse always builds exactly these 5 panels in a
    # known, fixed role (unlike the news pipeline's variable per-story panel
    # set), so a static 12-column grid needs no model sizing pass. Narrative
    # spans the top as a header row; velocity takes the left column below it;
    # the two metrics sit side by side in the top-right; oldest-open spans
    # beneath them at the same combined width. Verified no overlaps by hand.
    await helio.set_layout(dashboard_id, [
        {"panelId": narrative_panel_id, "x": 0, "y": 0, "w": 12, "h": 5},
        {"panelId": velocity_panel_id, "x": 0, "y": 5, "w": 6, "h": 9},
        {"panelId": cycle_time_panel_id, "x": 6, "y": 5, "w": 3, "h": 4},
        {"panelId": open_bugs_panel_id, "x": 9, "y": 5, "w": 3, "h": 4},
        {"panelId": oldest_panel_id, "x": 6, "y": 9, "w": 6, "h": 6},
    ])
