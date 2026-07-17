"""Dashboard-level "today at a glance" panels.

Unlike the REGISTRY enrichers these are plan-scoped, not story-scoped, so run.py
calls them directly rather than through `resolve()`. Everything here is measured
from the actual fetch — no model involvement, nothing invented — which makes it
the one section of the dashboard that is always correct and always populated.
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
    )


def volume_metric(plan, articles):
    """Articles scanned → single KPI tile."""
    from . import SourceData, T_STR, T_INT

    return SourceData(
        key="briefing-volume",
        columns=[{"name": "label", "type": T_STR},
                 {"name": "articles", "type": T_INT}],
        rows=[["Articles scanned", len(articles)]],
        mapping={"value": "articles", "label": "label"},
        panel_type="metric",
    )


def story_metric(plan, articles):
    """Stories surfaced → single KPI tile."""
    from . import SourceData, T_STR, T_INT

    return SourceData(
        key="briefing-stories",
        columns=[{"name": "label", "type": T_STR},
                 {"name": "stories", "type": T_INT}],
        rows=[["Stories today", len(plan.stories)]],
        mapping={"value": "stories", "label": "label"},
        panel_type="metric",
    )
