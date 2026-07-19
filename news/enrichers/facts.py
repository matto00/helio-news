"""facts:<mode> — the story's own key figures, pulled from its article bodies.

Unlike coverage/briefing (which count the *reporting*), this panel is about the
*substance*: the money, counts and percentages the story is actually about. The
numbers are produced upstream by the extractor→critic passes (news.agents) and
stashed on the StorySpec as `_facts` — each already grounded in a verbatim source
sentence and audited by the critic. This enricher just formats them; it invents
nothing and does no network I/O.

  facts:numbers   verified figures (label, value)   → table ("By the numbers")

Mirrors the planner-menu contract: `available()` says whether a story has enough
verified facts to be worth a panel, so the planner is only ever offered a key
that will really render.
"""

from __future__ import annotations

MIN_FACTS = 2


def _facts(story) -> list:
    return getattr(story, "_facts", None) or []


def build(arg, panel, story):
    from . import SourceData, T_STR

    mode = (arg or "numbers").strip().lower()
    if mode != "numbers":
        return None
    facts = _facts(story)
    if len(facts) < MIN_FACTS:
        return None

    rows = [[str(f.get("label", "")), str(f.get("value", ""))] for f in facts
            if f.get("label") and f.get("value")]
    if len(rows) < MIN_FACTS:
        return None

    return SourceData(
        key=f"facts-{story.slug}-numbers",
        columns=[{"name": "metric", "type": T_STR},
                 {"name": "value", "type": T_STR}],
        rows=rows,
        mapping={"columns": "metric,value"},
        panel_type="table",
    )


def available(story) -> bool:
    """Whether this story has enough verified figures for a By-the-numbers panel.
    Mirrors the threshold `build()` enforces; keep the two in step."""
    return len(_facts(story)) >= MIN_FACTS
