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
