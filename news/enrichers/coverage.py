"""coverage:<mode> — how the press is covering THIS story.

Derived entirely from the story's own clustered articles, so it costs no network
call and can never be stale or fabricated. It gives the planner something real to
visualize for stories that will never have a stock chart — which is most of them,
and exactly the stories that were rendering as a wall of markdown.

  coverage:sources   articles per outlet        → bar chart / table
  coverage:timeline  articles per hour (UTC)    → line/bar chart, shows a story breaking

A story needs at least MIN_SOURCES distinct outlets before `sources` is worth a
panel — a 1-outlet bar chart is just a number with extra steps.
"""

from __future__ import annotations

from collections import Counter

MIN_SOURCES = 3
MIN_TIMELINE_POINTS = 3


def _articles(story) -> list:
    return getattr(story, "_articles", None) or []


def build(arg, panel, story):
    from . import SourceData, T_STR, T_INT

    mode = (arg or "sources").strip().lower()
    arts = _articles(story)
    if not arts:
        return None

    if mode == "sources":
        counts = Counter(a.source for a in arts)
        if len(counts) < MIN_SOURCES:
            return None
        rows = [[src, n] for src, n in counts.most_common()]
        if panel.type == "table":
            return SourceData(
                key=f"coverage-{story.slug}-sources",
                columns=[{"name": "outlet", "type": T_STR},
                         {"name": "articles", "type": T_INT}],
                rows=rows,
                mapping={"columns": "outlet,articles"},
                panel_type="table",
            )
        return SourceData(
            key=f"coverage-{story.slug}-sources",
            columns=[{"name": "outlet", "type": T_STR},
                     {"name": "articles", "type": T_INT}],
            rows=rows,
            mapping={"xAxis": "outlet", "yAxis": "articles"},
            panel_type="chart",
            chart_type="bar",
        )

    if mode == "timeline":
        stamped = [a for a in arts if a.published]
        if len(stamped) < MIN_TIMELINE_POINTS:
            return None
        counts = Counter(a.published.strftime("%m-%d %H:00") for a in stamped)
        if len(counts) < MIN_TIMELINE_POINTS:
            return None
        rows = [[hour, n] for hour, n in sorted(counts.items())]
        return SourceData(
            key=f"coverage-{story.slug}-timeline",
            columns=[{"name": "hour", "type": T_STR},
                     {"name": "articles", "type": T_INT}],
            rows=rows,
            mapping={"xAxis": "hour", "yAxis": "articles"},
            panel_type="chart",
            chart_type="bar",
        )

    return None


def available(arts: list) -> list[str]:
    """Which coverage keys these articles actually support — used to build the
    planner's menu so it can only pick panels that will really render. Mirrors
    the thresholds `build()` enforces; keep the two in step."""
    out: list[str] = []
    if len({a.source for a in arts}) >= MIN_SOURCES:
        out.append("sources")
    stamped = {a.published.strftime("%m-%d %H:00") for a in arts if a.published}
    if len(stamped) >= MIN_TIMELINE_POINTS:
        out.append("timeline")
    return out
