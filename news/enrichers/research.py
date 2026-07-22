"""research:series — the agent-researched contextual series (formatting only).

The real work (the Claude + web-search call and the honesty gate) happens
upstream in `news.enrich`, which stashes a verified `Series` on the StorySpec as
`_research` — exactly the pattern `facts` uses with `_facts`. This enricher does
no network I/O and invents nothing: it just shapes the stored, already-grounded
series into a captioned trend line, so the planner is only ever offered a key
that will really render.
"""

from __future__ import annotations

import re


def _series(story):
    return getattr(story, "_research", None)


def build(arg, panel, story):
    from . import SourceData, T_STR, T_NUM

    series = _series(story)
    if series is None or len(getattr(series, "points", [])) < 2:
        return None

    rows = [[str(d), round(float(v), 4)] for d, v in series.points]
    host = re.sub(r"^https?://([^/]+).*", r"\1", series.url) if series.url else series.source
    return SourceData(
        key=f"research-{story.slug}",
        columns=[{"name": "date", "type": T_STR},
                 {"name": "value", "type": T_NUM}],
        rows=rows,
        mapping={"xAxis": "date", "yAxis": "value"},
        panel_type="chart",
        chart_type="line",
        chart_options={"smooth": True, "areaFill": True, "showPoints": False},
        annotation=f"Source: {series.source} ({host})",
    )


def available(story) -> bool:
    """Whether a verified researched series is stashed on this story."""
    s = _series(story)
    return s is not None and len(getattr(s, "points", [])) >= 2
