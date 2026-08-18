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
from . import history
from .agents import Ollama, curate, enrich, layout, sentiment_pass, timed, timings_report
from .enrichers import briefing
from .fetch import fetch_all, load_config
from .helio_client import HelioClient
from .history import HistoryEntry, write_day as history_write_day
from .plan_schema import DATA_PANEL_TYPES, DayPlan
from .projects.build import build_project_boards

REQUIRED_DELETE_TOOLS = {"delete_panel", "delete_data_source", "delete_data_type"}

GRID_COLS = 12

# Sizes used when the layout pass omits a panel or returns nothing usable.
# Deliberately dull — this is a safety net, not the design.
_FALLBACK = {
    "markdown": (6, 8),
    "chart": (6, 9),
    "metric": (3, 4),
    "table": (4, 7),
    "collection": (4, 6),
    "image": (6, 8),
}

# Floors and ceilings per panel kind: (min_w, min_h, max_h). The layout pass
# still chooses the size — these only stop a choice that can't render. The model
# is told charts need h≥6 and mostly complies, but it has handed back 6×3 bar
# charts, which clip their own axis labels.
_BOUNDS = {
    "chart": (4, 6, 24),
    "metric": (2, 3, 5),      # a single number never needs to be tall
    "collection": (3, 4, 24),  # a tile grid — wants some width to lay out cards
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


# ── dashboard routing ───────────────────────────────────────────────────────────
# The day splits across an overview board plus a few section boards (config
# `dashboards.sections`: board name → the domains it collects). Routing is
# deterministic from a story's domain; the curator only picks which stories lead
# the overview and writes each board's brief.


def _domain_to_board(config: dict) -> dict[str, str]:
    sections = config.get("dashboards", {}).get("sections", {})
    out: dict[str, str] = {}
    for board, domains in sections.items():
        for d in domains or []:
            out[str(d).strip().lower()] = board
    return out


def _board_slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


def _continuity_brief_fields(story) -> dict:
    """The curator fatigue signal for one story: {days_running, trend},
    both code-computed ground truth (news.history.ground_truth) — zeroed out
    when the historian/verifier passes didn't confirm a continuation."""
    continuity = getattr(story, "_continuity", None) or {}
    return {"days_running": continuity.get("days_running", 0),
           "trend": continuity.get("trend", "")}


def build_plan(config: dict):
    """Fetch → gemma passes (triage/planner/summarizer) → the two qwen editorial
    passes (sentiment, curator). Returns (plan, articles, curation)."""
    with timed("fetch"):
        articles = fetch_all(config)
    print(f"· fetched {len(articles)} fresh articles", file=sys.stderr)
    plan = enrich(articles, config, run_day=date.today())
    print(f"· gemma produced {len(plan.stories)} stories", file=sys.stderr)

    # ── editorial passes: sentiment + curator, both qwen, run back-to-back so the
    # bigger model is resident only once (the gemma passes above already ran).
    oc = config.get("ollama", {})
    models = config.get("models", {})
    effort = config.get("reasoning", {})
    ol = Ollama(oc.get("host", "http://localhost:11434"),
                oc.get("timeout_seconds", 180), oc.get("num_ctx"))

    with timed("sentiment"):
        tags = sentiment_pass(ol, models.get("sentiment", "gpt-oss:latest"),
                              plan.stories, effort.get("sentiment"))
    for s in plan.stories:
        s.sentiment = tags.get(s.slug, s.sentiment)
    pos = sum(1 for s in plan.stories if s.sentiment == "good")
    neg = sum(1 for s in plan.stories if s.sentiment == "bad")
    print(f"· sentiment: {pos} good, {neg} bad, "
          f"{len(plan.stories) - pos - neg} neutral", file=sys.stderr)

    dcfg = config.get("dashboards", {})
    routing = _domain_to_board(config)
    boards = list(dcfg.get("sections", {}).keys())
    stories_brief = [{
        "slug": s.slug, "board": routing.get(s.domain, dcfg.get("overview", "News Overview")),
        "headline": s.headline, "subject": s.subject, "importance": s.importance,
        "breaking": s.breaking, "summary": s.summary,
        **_continuity_brief_fields(s),
    } for s in plan.stories]
    with timed("curator"):
        curation = curate(ol, models.get("curator", "gpt-oss:latest"), stories_brief,
                          [dcfg.get("overview", "News Overview")] + boards,
                          int(dcfg.get("overview_size", 5)), effort.get("curator"))
    print(f"· curator: {len(curation.get('overview', []))} front-page picks, "
          f"{len(curation.get('briefs', {}))} briefs", file=sys.stderr)
    return plan, articles, curation


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


def _sentiment_color(sentiment: str, colors: dict) -> str:
    """Background tint for a story's data panels from its editorial sentiment."""
    return colors.get(sentiment, "") or ""


def _stock_move_color(sd, colors: dict) -> str:
    """A stock panel is coloured by its OWN price movement, not the story's mood —
    up = good/green, down = bad/red — read straight off the enricher's numbers."""
    try:
        if sd.key.endswith("-metric"):
            change = sd.rows[0][2]              # [label, price, change_pct]
        elif sd.key.endswith("-trend"):
            change = sd.rows[0][1]              # first bucket ("Day") % change
        else:                                   # price line: last vs first close
            change = sd.rows[-1][1] - sd.rows[0][1]
    except (IndexError, TypeError):
        return ""
    if change > 0:
        return colors.get("good", "") or ""
    if change < 0:
        return colors.get("bad", "") or ""
    return ""


def _panel_color(sd, story, colors: dict) -> str:
    """Pick a data panel's tint: stocks by their own movement, everything else by
    the story's sentiment. Only metric/chart panels are tinted (per the design)."""
    if sd.panel_type not in ("metric", "chart"):
        return ""
    if sd.key.startswith("stock-"):
        return _stock_move_color(sd, colors)
    return _sentiment_color(story.sentiment, colors)


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


async def _build_story(helio, dashboard_id: str, prefix: str, story, colors: dict,
                       seen: set[str], built: list[dict], *, digest: bool = False) -> None:
    """Render one story onto a board. A section board gets the full treatment
    (summary + photo + the planner's data panels, tinted by sentiment/movement);
    the overview gets a `digest` — summary + photo only, so headliner data panels
    aren't rebuilt off their section board.

    The hero image is emitted BEFORE the summary so it packs to the left of the
    summary on desktop and, when the grid linearises on mobile, sits ABOVE it."""
    arts = getattr(story, "_articles", None) or []

    if digest or any(p.type == "image" for p in story.panels):
        url = story.hero_image()
        if url:
            # Credit strip beneath the photo: the story's headline + the outlet
            # the image came from (v1.5 image caption).
            src = next((a.source for a in arts if getattr(a, "image_url", "")), "")
            caption = story.headline + (f" — {src}" if src else "")
            pid = await helio.add_image_panel(dashboard_id, story.title(), url,
                                              caption=caption)
            built.append({"id": pid, "kind": "image", "title": story.title(),
                          "importance": story.importance, "chart_type": None,
                          "note": "a photo"})

    md_id = await helio.add_text_panel(dashboard_id, story.title(), story_markdown(story))
    built.append({"id": md_id, "kind": "markdown", "title": story.title(),
                  "importance": story.importance, "chart_type": None,
                  "note": f"summary + {min(len(arts), 6)} headlines"})

    if digest:
        return

    for panel in story.panels:
        if panel.type == "image" or panel.type not in DATA_PANEL_TYPES:
            continue
        sd = enrichers.resolve(panel, story)
        if sd is None or sd.key in seen:
            continue
        seen.add(sd.key)
        color = _panel_color(sd, story, colors)
        pid = await helio.build_bound_panel(dashboard_id, prefix, panel.title, sd,
                                            background=color)
        built.append({"id": pid, "kind": sd.panel_type, "title": panel.title,
                      "importance": story.importance, "chart_type": sd.chart_type,
                      "note": f"{len(sd.rows)} rows"})


async def _finish_board(helio, dashboard_id: str, built: list[dict], config: dict) -> None:
    """Size (gemma layout pass) and place a board's panels. The editor's brief is
    forced to a slim full-width banner rather than whatever the model guesses."""
    if not built:
        return
    oc = config.get("ollama", {})
    ol = Ollama(oc.get("host", "http://localhost:11434"),
                oc.get("timeout_seconds", 180), oc.get("num_ctx"))
    manifest = [{"id": i, "kind": p["kind"], "title": p["title"],
                 "importance": p["importance"], "chart_type": p["chart_type"],
                 "note": p["note"]}
                for i, p in enumerate(built)]
    with timed("layout"):
        sizes = layout(ol, config.get("models", {}).get("layout", "gpt-oss:latest"), manifest,
                       config.get("reasoning", {}).get("layout"))
    await helio.set_layout(dashboard_id, _pack(built, sizes))


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

        prefix = plan.resource_prefix()

        # Route stories to their section board by domain (deterministic).
        by_board: dict[str, list] = {name: [] for name in section_names}
        for story in plan.stories:
            board = routing.get(story.domain)
            if board in by_board:
                by_board[board].append(story)

        # ── section boards: full, sentiment-tinted treatment ─────────────────
        for name in section_names:
            did = board_ids[name]
            built: list[dict] = []
            seen: set[str] = set()
            for story in sorted(by_board[name], key=lambda s: s.importance, reverse=True):
                await _build_story(helio, did, f"{prefix}-{_board_slug(name)}",
                                   story, colors, seen, built)
            await _finish_board(helio, did, built, config)
            print(f"· '{name}': {len(built)} panels / {len(by_board[name])} stories",
                  file=sys.stderr)

        # ── overview: brief + headliner digests + day-in-review ──────────────
        slug_to_story = {s.slug: s for s in plan.stories}
        picks = [slug_to_story[s] for s in curation.get("overview", [])
                 if s in slug_to_story]
        if not picks:                          # curator returned nothing usable
            picks = sorted(plan.stories, key=lambda s: s.importance, reverse=True)
        picks = picks[:overview_size]

        did = board_ids[overview_name]
        built = []
        seen = set()
        for story in picks:
            await _build_story(helio, did, f"{prefix}-overview", story, colors,
                               seen, built, digest=True)
        # Day-in-review: measured shape-of-the-day charts (never model-invented),
        # overview only. Neutral — these are meta, not good/bad news.
        for sd, title in ((briefing.domain_mix(plan), "What kind of day"),
                          (briefing.source_volume(plan, articles), "Who's publishing"),
                          (briefing.recap(plan, config), "This week")):
            if sd is None or sd.key in seen:
                continue
            seen.add(sd.key)
            pid = await helio.build_bound_panel(did, f"{prefix}-overview", title, sd)
            built.append({"id": pid, "kind": sd.panel_type, "title": title,
                          "importance": 2, "chart_type": sd.chart_type,
                          "note": "day in review"})
        await _finish_board(helio, did, built, config)
        print(f"· '{overview_name}': {len(built)} panels / {len(picks)} headliners",
              file=sys.stderr)
        await build_project_boards(config, helio, board_ids)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-only", action="store_true",
                    help="fetch + gemma only; print plan JSON; no helio calls")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the previous run's panels/sources")
    args = ap.parse_args(argv)

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
    # not fabricate multiple "days" of history). Non-fatal: history is the
    # least important output here — a disk error writing it must never abort
    # the actual dashboard build (write-before-push is still intentional, so
    # an MCP failure doesn't lose the day's record; only the write itself is
    # made fail-soft).
    hist_cfg = config.get("history", {})
    try:
        history_write_day(plan.day, [HistoryEntry.from_story(s, plan.day.isoformat())
                                     for s in plan.stories],
                          int(hist_cfg.get("retention_days", 60)))
    except Exception as e:
        print(f"· history write failed (non-fatal): {e}", file=sys.stderr)

    asyncio.run(apply_plan(plan, articles, config, curation, cleanup=not args.keep))
    print(timings_report(), file=sys.stderr)
    print("✅ dashboards refreshed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
