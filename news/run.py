"""Daily driver: fetch → gemma sequence → build the helio dashboard.

    python -m news.run                 # full run (builds/refreshes the dashboard)
    python -m news.run --plan-only      # fetch + gemma only, print the plan, no helio
    python -m news.run --keep           # skip cleanup of previous run's resources

The dashboard is refreshed in place: previous panels are cleared and the prior
run's dated sources/types are deleted, then today's are built fresh.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date

from . import enrichers
from .agents import enrich
from .fetch import fetch_all, load_config
from .helio_client import HelioClient
from .plan_schema import DATA_PANEL_TYPES, DayPlan

REQUIRED_DELETE_TOOLS = {"delete_panel", "delete_data_source", "delete_data_type"}


def build_plan(config: dict) -> DayPlan:
    articles = fetch_all(config)
    print(f"· fetched {len(articles)} fresh articles", file=sys.stderr)
    plan = enrich(articles, config, run_day=date.today())
    print(f"· gemma produced {len(plan.stories)} stories", file=sys.stderr)
    return plan


def plan_to_dict(plan: DayPlan) -> dict:
    return {
        "day": plan.day.isoformat(),
        "stories": [
            {**{k: v for k, v in asdict(s).items() if k != "panels"},
             "panels": [asdict(p) for p in s.panels]}
            for s in plan.stories
        ],
    }


def story_markdown(story) -> str:
    """One story's markdown panel body: summary + a linked headlines list. The
    section title is set on the panel, so it's deliberately NOT repeated here."""
    parts: list[str] = []
    if story.summary:
        parts.append(story.summary.strip())
    arts = getattr(story, "_articles", None) or []
    if arts:
        parts.append("\n**Headlines**")
        for a in arts[:6]:
            parts.append(f"- [{a.title}]({a.url}) — {a.source}")
    return "\n".join(parts) or story.headline


# 12-column grid. Panel heights are CONTENT-AWARE so nothing renders cramped or
# half-empty: a markdown panel grows with its headline count, charts get real
# vertical room, metrics stay compact tiles.
GRID_COLS = 12
H_MD_BASE = 3            # rows for the summary paragraph
H_PER_HEADLINE = 1      # + rows per headline line
H_MD_MIN, H_MD_MAX = 6, 15
H_CHART = 9
H_METRIC = 4
W_METRIC = 4


def _md_height(story) -> int:
    n_head = min(len(getattr(story, "_articles", None) or []), 6)
    return max(H_MD_MIN, min(H_MD_MAX, H_MD_BASE + n_head * H_PER_HEADLINE))


async def apply_plan(plan: DayPlan, config: dict, cleanup: bool = True) -> None:
    dash_name = config.get("helio", {}).get("dashboard_name", "News Overview")
    async with HelioClient.session(config) as helio:
        missing = REQUIRED_DELETE_TOOLS - await helio.tool_names()
        if missing:
            raise RuntimeError(
                f"helio MCP server is missing delete tools {missing}. Rebuild it "
                f"(helio-mcp: npm run build) so daily cleanup works."
            )

        dashboard_id = await helio.ensure_dashboard(dash_name)
        print(f"· dashboard '{dash_name}' → {dashboard_id}", file=sys.stderr)

        if cleanup:
            cleared = await helio.clear_dashboard_panels(dashboard_id)
            gone = await helio.cleanup_news_resources()
            print(f"· cleanup: {cleared} panels, {gone['sources']} sources, "
                  f"{gone['types']} types removed", file=sys.stderr)

        prefix = plan.resource_prefix()
        layout: list[dict] = []
        seen_tickers: set[str] = set()
        y = 0
        built = 0

        for story in sorted(plan.stories, key=lambda s: s.importance, reverse=True):
            # Always: one markdown panel (summary + headlines), titled by theme.
            md_id = await helio.add_text_panel(dashboard_id, story.title(),
                                               story_markdown(story))

            # Optional stock chart/metric, deduped so a ticker charts once total.
            chart_id = metric_id = None
            ticker = None
            for panel in story.panels:
                if panel.type not in DATA_PANEL_TYPES or panel.enricher() != "stock":
                    continue
                t = panel.data.split(":", 1)[1].upper()
                if t in seen_tickers:
                    continue
                sd = enrichers.resolve(panel, story)
                if sd is None:
                    continue
                title = f"{t} · 30-day price" if panel.type == "chart" else f"{t} · latest"
                pid = await helio.build_bound_panel(dashboard_id, prefix, title, sd)
                if panel.type == "chart":
                    chart_id = pid
                elif panel.type == "metric":
                    metric_id = pid
                ticker = t
                built += 1
            if ticker:
                seen_tickers.add(ticker)

            # Lay out this story's band with content-aware heights.
            has_stock = bool(chart_id or metric_id)
            h_md = _md_height(story)
            layout.append(_grid(md_id, 0, y, 6 if has_stock else GRID_COLS, h_md))
            right_h = 0
            if chart_id:
                layout.append(_grid(chart_id, 6, y, 6, H_CHART))
                right_h += H_CHART
            if metric_id:
                layout.append(_grid(metric_id, 6, y + right_h, W_METRIC, H_METRIC))
                right_h += H_METRIC
            # The band is as tall as its tallest column.
            y += max(h_md, right_h) if has_stock else h_md

        await helio.set_layout(dashboard_id, layout)
        print(f"· built {built} stock panels across {len(plan.stories)} stories; "
              f"laid out {len(layout)} panels", file=sys.stderr)


def _grid(panel_id: str, x: int, y: int, w: int, h: int) -> dict:
    return {"panelId": panel_id, "x": x, "y": y, "w": w, "h": h}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-only", action="store_true",
                    help="fetch + gemma only; print plan JSON; no helio calls")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the previous run's panels/sources")
    args = ap.parse_args(argv)

    config = load_config()
    plan = build_plan(config)

    if args.plan_only:
        print(json.dumps(plan_to_dict(plan), indent=2))
        return 0

    if not plan.stories:
        print("No stories produced; nothing to build.", file=sys.stderr)
        return 1

    asyncio.run(apply_plan(plan, config, cleanup=not args.keep))
    print("✅ dashboard refreshed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
