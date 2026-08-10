"""history:timeline — a verified multi-day continuity record for THIS story.

Built entirely from `story._continuity`, the dict news.agents.enrich()
attaches after the historian/verifier passes confirm a genuine continuation
(news.agents.continuity_facts) — this module invents nothing and does no
model or network I/O. A story needs at least MIN_OCCURRENCES verified past
appearances before the timeline is worth a panel (mirrors coverage.py's
"≥3 distinct hours" gate for its own timeline).

  history:timeline   the story's past occurrences, oldest matched first   → table
"""

from __future__ import annotations

MIN_OCCURRENCES = 3


def build(arg, panel, story):
    from . import SourceData, T_INT, T_STR

    continuity = getattr(story, "_continuity", None)
    if not continuity:
        return None
    occurrences = continuity.get("occurrences") or []
    if len(occurrences) < MIN_OCCURRENCES:
        return None

    rows = [[o["day"], o["headline"], o["importance"]] for o in occurrences]
    return SourceData(
        key=f"history-{story.slug}-timeline",
        columns=[{"name": "date", "type": T_STR},
                 {"name": "headline", "type": T_STR},
                 {"name": "importance", "type": T_INT}],
        rows=rows,
        mapping={"columns": "date,headline,importance"},
        panel_type="table",
        density="condensed",
        column_order=["date", "headline", "importance"],
    )
