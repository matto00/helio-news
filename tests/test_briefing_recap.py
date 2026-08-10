from datetime import date
from types import SimpleNamespace

import pytest

from news import history
from news.enrichers import briefing


@pytest.fixture(autouse=True)
def isolated_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")


def _story(slug, headline, importance, domain="markets"):
    return SimpleNamespace(slug=slug, headline=headline, subject="", domain=domain,
                           importance=importance, breaking=False, sentiment="neutral",
                           summary="", _articles=[])


def _plan(day, stories):
    return SimpleNamespace(day=day, stories=stories)


def _config(lookback_days=7, max_stories=6, match_threshold=0.3):
    return {"history": {"match_threshold": match_threshold,
                        "recap": {"lookback_days": lookback_days,
                                 "max_stories": max_stories}}}


def test_recap_none_with_no_history_at_all():
    plan = _plan(date(2026, 8, 9), [])
    assert briefing.recap(plan, _config()) is None


def test_recap_includes_todays_stories():
    plan = _plan(date(2026, 8, 9), [_story("padres-win", "Padres win the series", 3)])
    sd = briefing.recap(plan, _config())
    assert sd is not None
    assert sd.panel_type == "table"
    assert sd.rows == [["2026-08-09", "Padres win the series", 3]]


def test_recap_collapses_a_continuation_chain_to_its_peak():
    history.write_day(date(2026, 8, 7),
                      [history.HistoryEntry(slug="fed-a", headline="Fed weighs a rate cut",
                                            subject="", domain="markets", importance=2,
                                            breaking=False, sentiment="neutral", summary="",
                                            article_count=1, entities=["Federal Reserve"])],
                      retention_days=60)
    history.write_day(date(2026, 8, 8),
                      [history.HistoryEntry(slug="fed-b", headline="Fed signals a rate cut",
                                            subject="", domain="markets", importance=4,
                                            breaking=False, sentiment="neutral", summary="",
                                            article_count=1, entities=["Federal Reserve"])],
                      retention_days=60)
    plan = _plan(date(2026, 8, 9),
                [_story("fed-c", "Fed cuts rates a quarter point", 3)])
    sd = briefing.recap(plan, _config(match_threshold=0.2))
    assert len(sd.rows) == 1
    assert sd.rows[0][1] == "Fed signals a rate cut"   # the peak-importance entry (4)


def test_recap_caps_at_max_stories_by_peak_importance():
    # Headlines deliberately share NO significant tokens with each other (no
    # common filler words either) so group_entries keeps all 7 as separate
    # groups — this test is isolating the sort/cap step, not the clustering
    # step (that's covered by test_recap_collapses_a_continuation_chain_to_its_peak).
    headlines = [
        "Wildfire spreads across the valley",
        "Chess championship ends in stalemate",
        "New bakery opens downtown",
        "Marathon route changes for construction",
        "Aquarium welcomes a baby otter",
        "Bridge repairs begin next month",
        "Local choir wins a regional award",
    ]
    # Importances 1..7 — real StorySpec clamps 1-5, but this SimpleNamespace
    # fixture bypasses that validation deliberately, to isolate the sort/cap
    # logic with unambiguous, distinct values.
    stories = [_story(f"s{i}", h, i + 1, domain="general")
              for i, h in enumerate(headlines)]
    plan = _plan(date(2026, 8, 9), stories)
    sd = briefing.recap(plan, _config(max_stories=3))
    assert len(sd.rows) == 3
    assert [r[2] for r in sd.rows] == [7, 6, 5]   # highest peak importance first


def test_recap_returns_none_on_malformed_stored_importance():
    # A hand-edited or externally corrupted history file with a non-numeric
    # importance (e.g. `"importance": "4"`) must not crash the whole overview
    # board — recap() should fail soft and return None, not raise, when the
    # bad type blows up a later sort/max comparison.
    history.write_day(
        date(2026, 8, 7),
        [history.HistoryEntry(slug="corrupt", headline="Quarterly earnings beat expectations",
                              subject="", domain="markets", importance="oops",
                              breaking=False, sentiment="neutral", summary="",
                              article_count=1, entities=[])],
        retention_days=60)
    plan = _plan(date(2026, 8, 9),
                [_story("today", "Wildfire spreads across the valley", 3)])
    assert briefing.recap(plan, _config()) is None
