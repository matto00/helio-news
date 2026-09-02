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
    mapping: dict[str, str]                     # Output config.fieldMapping (set via update_output)
    panel_type: str                             # metric | chart | table | collection
    # line | bar | pie | scatter. The enricher picks the shape that suits its
    # own data (a % -change comparison is a bar, a price history is a line);
    # the planner may override it. Ignored for non-chart panels.
    chart_type: str | None = None

    # ── v1.5 subtype config (now lives on the Output, set via update_output) ──
    # All optional; each is consumed only for the panel_type it belongs to, so an
    # enricher sets just the ones that apply to its own shape.
    chart_options: dict | None = None          # chart: config.chartOptions (per chart type)
    annotation: str | None = None              # chart: config.annotation — source-attribution footnote
    base_type: str | None = None               # collection: config.baseType (e.g. "metric")
    layout: str | None = None                  # collection: config.layout (grid|list)
    density: str | None = None                 # table: config.density (condensed|normal|spacious)
    column_order: list[str] | None = None      # table: config.columnOrder (visible cols, in order)

    # Pipeline transform steps to apply over the source, in order, as
    # [{"type": "aggregate"|"sort"|…, "config": {…}}]. Default (None) = a single
    # identity `select` of all columns (today's behaviour). An enricher sets this
    # to make helio do real work — e.g. groupBy month + avg to reduce a daily
    # series to a monthly trend. The `mapping` must reference the FINAL output
    # columns these steps produce, not the source columns.
    steps: list[dict] | None = None

    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]

    def pipeline_steps(self) -> list[dict]:
        """The transform steps to add to this panel's pipeline — the enricher's
        own `steps` if given, else a single identity select of every column."""
        return self.steps or [{"type": "select",
                               "config": {"fields": self.column_names()}}]

    def panel_config(self) -> dict:
        """The Output `config` for this panel_type — v1.5 subtype config
        (collection base/layout, chart display options + annotation, table
        density/order). Empty when nothing applies (metric, or an unadorned
        chart), so the caller can omit `config` entirely."""
        cfg: dict = {}
        if self.panel_type == "collection":
            cfg["baseType"] = self.base_type or "metric"
            cfg["layout"] = self.layout or "grid"
        elif self.panel_type == "chart":
            # chartOptions is keyed BY CHART TYPE ({bar:{…}} / {line:{…}}); a flat
            # dict is silently dropped by the backend. Nest under the final
            # chart_type (which the planner may have overridden via resolve()).
            if self.chart_options and self.chart_type:
                cfg["chartOptions"] = {self.chart_type: self.chart_options}
            if self.annotation:
                cfg["annotation"] = self.annotation
        elif self.panel_type == "table":
            if self.density:
                cfg["density"] = self.density
            if self.column_order:
                cfg["columnOrder"] = self.column_order
        return cfg


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
from . import series as _series         # noqa: E402
from . import research as _research     # noqa: E402
from . import history as _history       # noqa: E402

# Prefix → builder. Add a new capability here (e.g. "sports": _sports.build).
# Headlines/summaries are not enrichers — run.py renders them into each story's
# markdown panel directly. Neither are images: they're unbound content panels.
REGISTRY = {
    "stock": _stocks.build,
    "coverage": _coverage.build,
    "facts": _facts.build,
    "series": _series.build,
    "research": _research.build,
    "history": _history.build,
}
