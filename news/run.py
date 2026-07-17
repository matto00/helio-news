"""Daily driver: fetch → gemma sequence → build the helio dashboard.

    python -m news.run                 # full run (builds/refreshes the dashboard)
    python -m news.run --plan-only      # fetch + gemma only, print the plan, no helio
    python -m news.run --keep           # skip cleanup of previous run's resources

The dashboard is refreshed in place: previous panels are cleared and the prior
run's sources/types are deleted, then today's are built fresh.

Layout is decided by the `layout` gemma pass, not by this file: it sizes every
panel (w × h) and `_pack` flows those sizes into non-overlapping grid positions.
The split matters — the model is good at "this lead story deserves a big panel"
and bad at emitting 30 non-overlapping rectangles, so it does the judgement and
code does the geometry. If the pass fails or skips a panel, `_FALLBACK` sizes it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date

from . import enrichers
from .agents import Ollama, enrich, layout
from .enrichers import briefing
from .fetch import fetch_all, load_config
from .helio_client import HelioClient
from .plan_schema import DATA_PANEL_TYPES, DayPlan

REQUIRED_DELETE_TOOLS = {"delete_panel", "delete_data_source", "delete_data_type"}

GRID_COLS = 12

# Sizes used when the layout pass omits a panel or returns nothing usable.
# Deliberately dull — this is a safety net, not the design.
_FALLBACK = {
    "markdown": (6, 8),
    "chart": (6, 9),
    "metric": (3, 4),
    "table": (4, 7),
    "image": (6, 8),
}

# Floors and ceilings per panel kind: (min_w, min_h, max_h). The layout pass
# still chooses the size — these only stop a choice that can't render. The model
# is told charts need h≥6 and mostly complies, but it has handed back 6×3 bar
# charts, which clip their own axis labels.
_BOUNDS = {
    "chart": (4, 6, 24),
    "metric": (2, 3, 5),      # a single number never needs to be tall
    "image": (3, 5, 24),
    "table": (3, 4, 24),
    "markdown": (3, 5, 24),
}

# Don't stretch a shelf that's mostly empty — a lone 3-wide metric should stay a
# tile, not balloon to full width. At this fill or above, the leftover is a
# ragged edge worth closing rather than deliberate whitespace.
_FILL_THRESHOLD = 7


def _clamp(kind: str, w: int, h: int) -> tuple[int, int]:
    min_w, min_h, max_h = _BOUNDS.get(kind, (1, 2, 24))
    return (max(min_w, min(GRID_COLS, w)), max(min_h, min(max_h, h)))


def build_plan(config: dict):
    articles = fetch_all(config)
    print(f"· fetched {len(articles)} fresh articles", file=sys.stderr)
    plan = enrich(articles, config, run_day=date.today())
    print(f"· gemma produced {len(plan.stories)} stories", file=sys.stderr)
    return plan, articles


def plan_to_dict(plan: DayPlan) -> dict:
    return {
        "day": plan.day.isoformat(),
        "stories": [
            {**{k: v for k, v in asdict(s).items() if k != "panels"},
             "image": s.hero_image(),
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


def _fill_shelf(shelf: list[dict]) -> None:
    """Widen a nearly-full shelf's panels to close the ragged right edge.

    The layout model sizes each panel without knowing where rows will break, so
    shelves land on 10 or 11 of 12 columns and leave a dead strip down the side.
    Widening proportionally keeps the model's *relative* sizing — its actual
    judgement — while making the row flush. Mutates in place."""
    used = sum(p["w"] for p in shelf)
    if not shelf or used >= GRID_COLS or used < _FILL_THRESHOLD:
        return
    for p in shelf:
        p["w"] = max(1, round(p["w"] * GRID_COLS / used))
    # Rounding can overshoot or undershoot; settle up on the widest panel.
    drift = GRID_COLS - sum(p["w"] for p in shelf)
    if drift:
        widest = max(shelf, key=lambda p: p["w"])
        widest["w"] = max(1, widest["w"] + drift)
    x = 0
    for p in shelf:
        p["x"] = x
        x += p["w"]


def _pack(built: list[dict], sizes: dict[int, tuple[int, int]]) -> list[dict]:
    """Flow sized panels across the 12-column grid, left→right, wrapping to a new
    shelf when a row is full. Order is preserved, so the plan's importance
    ordering survives, and overlaps are impossible by construction."""
    items: list[dict] = []
    shelf: list[dict] = []
    x = y = shelf_h = 0
    for i, p in enumerate(built):
        w, h = sizes.get(i) or _FALLBACK.get(p["kind"], (6, 8))
        w, h = _clamp(p["kind"], w, h)
        if x + w > GRID_COLS:
            _fill_shelf(shelf)
            shelf = []
            x, y, shelf_h = 0, y + shelf_h, 0
        item = {"panelId": p["id"], "x": x, "y": y, "w": w, "h": h}
        items.append(item)
        shelf.append(item)
        x += w
        shelf_h = max(shelf_h, h)
    _fill_shelf(shelf)
    return items


async def apply_plan(plan: DayPlan, articles: list, config: dict,
                     cleanup: bool = True) -> None:
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
        built: list[dict] = []          # ordered manifest: what exists on the board
        seen_keys: set[str] = set()     # dedupe identical data across stories

        async def add_bound(sd, title: str, importance: int, note: str) -> None:
            if sd is None or sd.key in seen_keys:
                return
            seen_keys.add(sd.key)
            pid = await helio.build_bound_panel(dashboard_id, prefix, title, sd)
            built.append({"id": pid, "kind": sd.panel_type, "title": title,
                          "importance": importance, "chart_type": sd.chart_type,
                          "note": note})

        # ── today at a glance ────────────────────────────────────────────────
        # Real measurements of the fetch, so this section is always populated and
        # never model-invented. It also gives the board its metric/pie variety.
        for sd, title, note in (
            (briefing.volume_metric(plan, articles), "Articles scanned", "one number"),
            (briefing.story_metric(plan, articles), "Stories today", "one number"),
            (briefing.domain_mix(plan), "What kind of day", "pie, few slices"),
            (briefing.source_volume(plan, articles), "Who's publishing", "bar chart"),
        ):
            await add_bound(sd, title, 3, note)

        # ── per-story sections ───────────────────────────────────────────────
        for story in sorted(plan.stories, key=lambda s: s.importance, reverse=True):
            arts = getattr(story, "_articles", None) or []
            md_id = await helio.add_text_panel(dashboard_id, story.title(),
                                               story_markdown(story))
            built.append({"id": md_id, "kind": "markdown", "title": story.title(),
                          "importance": story.importance, "chart_type": None,
                          "note": f"summary + {min(len(arts), 6)} headlines"})

            for panel in story.panels:
                if panel.type == "image":
                    url = story.hero_image()
                    if url:
                        pid = await helio.add_image_panel(dashboard_id, panel.title, url)
                        built.append({"id": pid, "kind": "image", "title": panel.title,
                                      "importance": story.importance,
                                      "chart_type": None, "note": "a photo"})
                    continue
                if panel.type not in DATA_PANEL_TYPES:
                    continue
                sd = enrichers.resolve(panel, story)
                await add_bound(sd, panel.title, story.importance,
                                f"{len(sd.rows)} rows" if sd else "")

        # ── the layout pass sizes what we actually built ─────────────────────
        oc = config.get("ollama", {})
        ol = Ollama(oc.get("host", "http://localhost:11434"), oc.get("timeout_seconds", 180))
        manifest = [{"id": i, "kind": p["kind"], "title": p["title"],
                     "importance": p["importance"], "chart_type": p["chart_type"],
                     "note": p["note"]}
                    for i, p in enumerate(built)]
        sizes = layout(ol, config.get("models", {}).get("layout", "gemma4:e4b"), manifest)
        print(f"· layout pass sized {len(sizes)}/{len(built)} panels", file=sys.stderr)

        await helio.set_layout(dashboard_id, _pack(built, sizes))

        kinds = ", ".join(f"{k}×{sum(1 for p in built if p['kind'] == k)}"
                          for k in sorted({p["kind"] for p in built}))
        print(f"· built {len(built)} panels across {len(plan.stories)} stories "
              f"({kinds})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-only", action="store_true",
                    help="fetch + gemma only; print plan JSON; no helio calls")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the previous run's panels/sources")
    args = ap.parse_args(argv)

    config = load_config()
    plan, articles = build_plan(config)

    if args.plan_only:
        print(json.dumps(plan_to_dict(plan), indent=2))
        return 0

    if not plan.stories:
        print("No stories produced; nothing to build.", file=sys.stderr)
        return 1

    asyncio.run(apply_plan(plan, articles, config, cleanup=not args.keep))
    print("✅ dashboard refreshed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
