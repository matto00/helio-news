"""Pluggable aux-data enrichers.

Each panel the planner emits carries a ``data`` key ``"<enricher>:<arg>"``. An
enricher turns that into a `SourceData` — helio columns + rows plus the field
mapping helio needs to bind the panel. `run.py` creates one static source +
trivial pipeline per `SourceData`, then binds the panel to the pipeline output.

Adding a new capability (e.g. sports rosters) = write `sports.py`, register its
prefix in `REGISTRY`, and teach the planner prompt the new key. Nothing else
changes. Any resolve() that returns None simply drops that panel (the story
still has its headlines fallback), so enrichment never breaks a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..plan_schema import PanelSpec

# Centralised helio column-type names so a vocabulary tweak is one edit.
# (Verified against the live helio backend during scaffold build.)
T_STR = "string"
T_INT = "integer"
T_NUM = "double"


@dataclass
class SourceData:
    """A self-contained helio static-source payload for one panel."""

    key: str                                   # stable, name-safe id fragment
    columns: list[dict]                         # [{"name","type"}, ...]
    rows: list[list]                            # row-major values
    mapping: dict[str, str]                     # bind_panel fieldMapping
    panel_type: str                             # metric | chart | table
    # line | bar | pie | scatter. The enricher picks the shape that suits its
    # own data (a % -change comparison is a bar, a price history is a line);
    # the planner may override it. Ignored for non-chart panels.
    chart_type: str | None = None

    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]


def resolve(panel: PanelSpec, story) -> SourceData | None:
    """Dispatch a data panel to its enricher. `story` is a StorySpec (carries the
    clustered `_articles`). Returns None — dropping the panel — rather than
    raising, so one bad symbol or empty feed never takes down a run."""
    enr = panel.enricher()
    arg = panel.data.split(":", 1)[1] if panel.data and ":" in panel.data else ""
    fn = REGISTRY.get(enr or "")
    if not fn:
        return None
    try:
        sd = fn(arg, panel, story)
    except Exception:
        return None
    if sd and panel.chart_type and sd.panel_type == "chart":
        sd.chart_type = panel.chart_type      # planner's choice wins
    return sd


# Imported after SourceData is defined to avoid circular import surprises.
from . import coverage as _coverage     # noqa: E402
from . import stocks as _stocks         # noqa: E402
from . import facts as _facts           # noqa: E402

# Prefix → builder. Add a new capability here (e.g. "sports": _sports.build).
# Headlines/summaries are not enrichers — run.py renders them into each story's
# markdown panel directly. Neither are images: they're unbound content panels.
REGISTRY = {
    "stock": _stocks.build,
    "coverage": _coverage.build,
    "facts": _facts.build,
}
