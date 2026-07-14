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

    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]


def resolve(panel: PanelSpec, story) -> SourceData | None:
    """Dispatch a data panel to its enricher. `story` is a StorySpec (carries the
    clustered `_articles` for the headlines enricher)."""
    enr = panel.enricher()
    arg = panel.data.split(":", 1)[1] if panel.data and ":" in panel.data else ""
    fn = REGISTRY.get(enr or "")
    if not fn:
        return None
    try:
        return fn(arg, panel, story)
    except Exception:
        return None


# Imported after SourceData is defined to avoid circular import surprises.
from . import stocks as _stocks         # noqa: E402

# Prefix → builder. Add a new capability here (e.g. "sports": _sports.build).
# Headlines/summaries are not enrichers — run.py renders them into each story's
# markdown panel directly.
REGISTRY = {
    "stock": _stocks.build,
}
