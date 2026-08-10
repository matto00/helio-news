"""Dashboard-level "day in review" panels.

Unlike the REGISTRY enrichers these are plan-scoped, not story-scoped, so run.py
calls them directly rather than through `resolve()`. Everything here is measured
from the actual fetch — no model involvement, nothing invented.

Scope note: the bare count tiles ("Articles scanned", "Stories today") were
removed deliberately — the dashboards are meant to be dynamic reporting on the
stories themselves, not a scoreboard of how many there were. What remains are the
two shape-of-the-day charts, shown only on the overview board.
"""

from __future__ import annotations

from collections import Counter

TOP_SOURCES = 8


def domain_mix(plan):
    """Stories per domain → pie. What kind of day was it?"""
    from . import SourceData, T_STR, T_INT

    counts = Counter(s.domain for s in plan.stories)
    if len(counts) < 2:
        return None
    return SourceData(
        key="briefing-domains",
        columns=[{"name": "domain", "type": T_STR},
                 {"name": "stories", "type": T_INT}],
        rows=[[d, n] for d, n in counts.most_common()],
        mapping={"xAxis": "domain", "yAxis": "stories"},
        panel_type="chart",
        chart_type="pie",
        chart_options={"donutHolePct": 45, "showPercentLabels": True},
    )


def source_volume(plan, articles):
    """Articles scanned per outlet → bar. Shows which feeds are actually
    carrying the day, and makes a dead feed visible at a glance."""
    from . import SourceData, T_STR, T_INT

    counts = Counter(a.source for a in articles)
    if len(counts) < 2:
        return None
    return SourceData(
        key="briefing-sources",
        columns=[{"name": "outlet", "type": T_STR},
                 {"name": "articles", "type": T_INT}],
        rows=[[s, n] for s, n in counts.most_common(TOP_SOURCES)],
        mapping={"xAxis": "outlet", "yAxis": "articles"},
        panel_type="chart",
        chart_type="bar",
        # Outlet names are long — a horizontal bar reads them without rotating.
        chart_options={"orientation": "horizontal"},
    )


def recap(plan, config):
    """The past week's top stories, independent of today's news — a
    deterministic aggregation, no model call. A multi-day continuation chain
    (news.history.group_entries) counts once, at its peak importance, so a
    story that led for four straight days doesn't crowd out the rest of the
    week. Overview-board only.

    Called directly from run.py's apply_plan() — NOT through the enrichers'
    resolve() wrapper, which is what normally catches a bad-data exception
    for a REGISTRY enricher. load_window() skips malformed FILES but doesn't
    validate FIELD TYPES, so a hand-edited/corrupted state/history/*.json
    (e.g. a string "importance") would otherwise raise deep in a sort/max
    call and crash the entire overview board, not just drop this panel.
    Mirrors enrichers.resolve()'s try/except-return-None pattern locally so
    that failure mode degrades to "no recap panel today" instead."""
    try:
        from . import SourceData, T_INT, T_STR
        from .. import history as _history

        hist_cfg = config.get("history", {})
        recap_cfg = hist_cfg.get("recap", {})
        lookback = int(recap_cfg.get("lookback_days", 7))
        max_stories = int(recap_cfg.get("max_stories", 6))
        threshold = float(hist_cfg.get("match_threshold", 0.35))

        window = _history.load_window(plan.day, lookback)
        today_entries = [_history.HistoryEntry.from_story(s, plan.day.isoformat())
                         for s in plan.stories]
        all_entries = window + today_entries
        if not all_entries:
            return None

        groups = _history.group_entries(all_entries, threshold)
        peaks = [max(g, key=lambda e: e.importance) for g in groups]
        peaks.sort(key=lambda e: e.importance, reverse=True)
        top = peaks[:max_stories]
        if not top:
            return None

        return SourceData(
            key="briefing-recap",
            columns=[{"name": "date", "type": T_STR},
                     {"name": "headline", "type": T_STR},
                     {"name": "importance", "type": T_INT}],
            rows=[[e.day, e.headline, e.importance] for e in top],
            mapping={"columns": "date,headline,importance"},
            panel_type="table",
            density="condensed",
            column_order=["date", "headline", "importance"],
        )
    except Exception:
        return None
