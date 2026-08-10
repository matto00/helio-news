from types import SimpleNamespace

from news.enrichers import history as history_enricher


def _story(continuity=None, slug="fed-rate-cut"):
    return SimpleNamespace(slug=slug, _continuity=continuity)


def test_build_returns_none_without_continuity():
    assert history_enricher.build("timeline", None, _story(continuity=None)) is None


def test_build_returns_none_below_min_occurrences():
    continuity = {"occurrences": [{"day": "2026-08-08", "headline": "x", "importance": 3},
                                  {"day": "2026-08-07", "headline": "y", "importance": 2}]}
    assert history_enricher.build("timeline", None, _story(continuity=continuity)) is None


def test_build_returns_table_when_confirmed():
    continuity = {"occurrences": [
        {"day": "2026-08-06", "headline": "Fed weighs a rate cut", "importance": 2},
        {"day": "2026-08-07", "headline": "Fed signals a rate cut", "importance": 3},
        {"day": "2026-08-08", "headline": "Fed set to cut rates", "importance": 3},
    ]}
    sd = history_enricher.build("timeline", None, _story(continuity=continuity))
    assert sd is not None
    assert sd.panel_type == "table"
    assert sd.key == "history-fed-rate-cut-timeline"
    assert len(sd.rows) == 3
    assert sd.rows[0] == ["2026-08-06", "Fed weighs a rate cut", 2]
    assert sd.column_order == ["date", "headline", "importance"]


def test_registered_in_registry_and_known_enrichers():
    from news import enrichers, plan_schema

    assert "history" in enrichers.REGISTRY
    assert enrichers.REGISTRY["history"] is history_enricher.build
    assert "history" in plan_schema.KNOWN_ENRICHERS
