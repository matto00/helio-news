"""series:<provider>:<id>[:<mode>] — the story's quantity in real context.

The panel that puts a story on a curve. When a story is *about* a trackable
quantity (inflation, gas, oil, unemployment, mortgage rates), the config
series-map points at a REAL public series for it, and this enricher fetches that
series from the provider (FRED / Yahoo) and hands it to helio as a trend chart —
captioned with its source, never invented.

  series:fred:CPIAUCSL            native-fidelity trend line (as the source reports it)
  series:yahoo:CL=F:monthly       daily series reduced to a MONTHLY AVERAGE by a
                                  helio pipeline (groupBy month → avg) — "varying
                                  fidelity" done where it belongs, in the pipeline

Mode:
  (default / "trend") — upload the points as-is; identity pipeline; line chart.
  "monthly"           — upload the raw points plus a month key; the pipeline
                        aggregates them to a monthly average. Use for dense
                        (daily/weekly) series where the raw line is unreadable.

Honesty invariant: the numbers come only from a provider fetch. A failed/gated
fetch (e.g. no FRED key) returns None and the panel is dropped — nothing here is
model-authored, and every chart carries its source in the annotation footnote.
"""

from __future__ import annotations

import re

from ..providers import fred as _fred
from ..providers import yahoo as _yahoo

PROVIDERS = {"fred": _fred, "yahoo": _yahoo}

_LINE_OPTS = {"smooth": True, "areaFill": True, "showPoints": False}


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def build(arg, panel, story):
    from . import SourceData, T_STR, T_NUM

    # arg is "provider:id[:mode]" (resolve() already stripped the "series:" prefix).
    parts = [p.strip() for p in (arg or "").split(":") if p.strip()]
    if len(parts) < 2:
        return None
    provider, series_id = parts[0].lower(), parts[1]
    mode = parts[2].lower() if len(parts) > 2 else "trend"

    prov = PROVIDERS.get(provider)
    if prov is None:
        return None
    series = prov.fetch(series_id)
    if series is None or len(series.points) < 2:
        return None

    key = f"series-{provider}-{_safe(series_id)}-{mode}"
    attribution = f"{series.attribution()} — {series_id}"

    if mode == "monthly":
        # Upload every raw point with a month key; the PIPELINE averages per month
        # (groupBy month → avg). One upload, reduced fidelity, done in helio.
        rows = [[d, d[:7], round(v, 4)] for d, v in series.points]
        return SourceData(
            key=key,
            columns=[{"name": "date", "type": T_STR},
                     {"name": "month", "type": T_STR},
                     {"name": "value", "type": T_NUM}],
            rows=rows,
            mapping={"xAxis": "month", "yAxis": "avg_value"},
            panel_type="chart",
            chart_type="line",
            chart_options=_LINE_OPTS,
            annotation=attribution,
            steps=[
                {"type": "aggregate", "config": {
                    "groupBy": [{"name": "month", "type": "string"}],
                    "aggregations": [{"alias": "avg_value", "field": "value", "fn": "avg"}],
                }},
                {"type": "sort", "config": {
                    "sortBy": [{"direction": "asc", "field": "month"}],
                }},
            ],
        )

    # Default: native-fidelity trend line (identity pipeline).
    rows = [[d, round(v, 4)] for d, v in series.points]
    return SourceData(
        key=key,
        columns=[{"name": "date", "type": T_STR},
                 {"name": "value", "type": T_NUM}],
        rows=rows,
        mapping={"xAxis": "date", "yAxis": "value"},
        panel_type="chart",
        chart_type="line",
        chart_options=_LINE_OPTS,
        annotation=attribution,
    )
