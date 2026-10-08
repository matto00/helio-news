from news.enrichers import SourceData

# Mirror of helio's per-kind Output config allowlist, from
# backend/src/main/scala/com/helio/services/pipelines/OutputConfigValidation.scala
# (HEL-1313): unknown keys are rejected with 400. Keep in sync with helio.
_SHARED = {"fieldMapping", "compare", "historyPayloads"}
_ALLOWED = {
    "chart": _SHARED | {"chartType", "aggregation", "chartOptions", "annotation"},
    "metric": _SHARED | {"aggregation", "label", "unit", "format"},
    "table": _SHARED | {"columnOrder", "columnFormats", "columnSort", "columnFilters", "pinnedColumns"},
    "collection": _SHARED | {"layout", "format"},
}


def _sd(panel_type, **kw):
    return SourceData(key="k", columns=[{"name": "a", "type": "string"}],
                      rows=[["x"]], mapping={}, panel_type=panel_type, **kw)


def test_panel_config_only_emits_keys_helio_accepts():
    cases = [
        _sd("table", column_order=["a"]),
        _sd("table"),
        _sd("collection", layout="grid"),
        _sd("collection"),
        _sd("chart", chart_type="bar", chart_options={"stacked": True}, annotation="src"),
        _sd("metric"),
    ]
    for sd in cases:
        extra = set(sd.panel_config()) - _ALLOWED[sd.panel_type]
        assert not extra, f"{sd.panel_type} emits keys helio rejects: {extra}"


def test_collection_and_table_config_values():
    assert _sd("collection", layout="list").panel_config() == {"layout": "list"}
    assert _sd("table", column_order=["a"]).panel_config() == {"columnOrder": ["a"]}
