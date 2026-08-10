from datetime import date

import pytest

from news import history


@pytest.fixture(autouse=True)
def isolated_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    return tmp_path / "history"


def _entry(slug="fed-rate-cut", headline="Fed cuts rates a quarter point",
           subject="Federal Reserve rate policy", domain="markets",
           importance=4, day=""):
    return history.HistoryEntry(
        slug=slug, headline=headline, subject=subject, domain=domain,
        importance=importance, breaking=False, sentiment="neutral",
        summary="The Fed cut rates.", article_count=5,
        entities=["Federal Reserve"], day=day,
    )


def test_write_day_creates_one_file_per_day(isolated_history_dir):
    history.write_day(date(2026, 8, 9), [_entry()], retention_days=60)
    f = isolated_history_dir / "2026-08-09.json"
    assert f.exists()
    payload = f.read_text(encoding="utf-8")
    assert "fed-rate-cut" in payload
    assert '"day"' not in payload   # day is implied by the filename


def test_write_day_prunes_files_older_than_retention(isolated_history_dir):
    isolated_history_dir.mkdir(parents=True)
    (isolated_history_dir / "2026-01-01.json").write_text("[]", encoding="utf-8")
    history.write_day(date(2026, 8, 9), [_entry()], retention_days=60)
    assert not (isolated_history_dir / "2026-01-01.json").exists()
    assert (isolated_history_dir / "2026-08-09.json").exists()


def test_load_window_excludes_today_and_out_of_range_days(isolated_history_dir):
    history.write_day(date(2026, 8, 2), [_entry(slug="week-old")], retention_days=60)
    history.write_day(date(2026, 8, 8), [_entry(slug="yesterday")], retention_days=60)
    # A file for "today" (2026-08-09) should never be read by load_window,
    # since it represents the run currently being built.
    isolated_history_dir_path = isolated_history_dir
    (isolated_history_dir_path / "2026-08-09.json").write_text(
        history._serialize([_entry(slug="today")]), encoding="utf-8")

    window = history.load_window(date(2026, 8, 9), lookback_days=7)
    slugs = {e.slug for e in window}
    assert slugs == {"yesterday"}   # 2026-08-02 is 7 days back — out of a 7-day window; today excluded


def test_load_window_skips_malformed_files(isolated_history_dir):
    isolated_history_dir.mkdir(parents=True)
    (isolated_history_dir / "2026-08-08.json").write_text("not json", encoding="utf-8")
    (isolated_history_dir / "not-a-date.json").write_text("[]", encoding="utf-8")
    window = history.load_window(date(2026, 8, 9), lookback_days=7)
    assert window == []


def test_load_window_skips_invalid_utf8_files(isolated_history_dir):
    isolated_history_dir.mkdir(parents=True)
    # Write a file with invalid UTF-8 bytes
    (isolated_history_dir / "2026-08-08.json").write_bytes(b'\xff\xfe\x00 invalid utf8 \x80\x81')
    # Should not raise UnicodeDecodeError; should skip the file gracefully
    window = history.load_window(date(2026, 8, 9), lookback_days=7)
    assert window == []


def test_load_window_returns_no_files_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "does-not-exist")
    assert history.load_window(date(2026, 8, 9), lookback_days=7) == []


def test_find_candidates_matches_on_shared_headline_tokens():
    window = [
        history.HistoryEntry(
            slug="fed-considers-cut", headline="Fed considers a quarter-point rate cut",
            subject="Federal Reserve", domain="markets", importance=3,
            breaking=False, sentiment="neutral", summary="", article_count=3,
            entities=["Federal Reserve"], day="2026-08-06",
        ),
        history.HistoryEntry(
            slug="padres-win", headline="Padres win 4-2 over the Giants",
            subject="Padres", domain="sports", importance=2, breaking=False,
            sentiment="good", summary="", article_count=2, entities=[],
            day="2026-08-06",
        ),
    ]
    candidates = history.find_candidates(
        "Fed cuts rates a quarter point", "Federal Reserve rate policy",
        ["Federal Reserve"], window, threshold=0.3)
    assert [c.entry.slug for c in candidates] == ["fed-considers-cut"]
    assert candidates[0].score > 0


def test_find_candidates_respects_threshold():
    window = [history.HistoryEntry(
        slug="loose-overlap", headline="A story about the general economy",
        subject="", domain="business", importance=3, breaking=False,
        sentiment="neutral", summary="", article_count=1, entities=[],
        day="2026-08-06",
    )]
    candidates = history.find_candidates(
        "Fed cuts rates a quarter point", "", [], window, threshold=0.9)
    assert candidates == []


def test_find_candidates_empty_window_or_blank_story_returns_empty():
    assert history.find_candidates("Fed cuts rates", "", [], [], threshold=0.3) == []
    entry = history.HistoryEntry(
        slug="x", headline="Fed cuts rates", subject="", domain="markets",
        importance=3, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day="2026-08-06")
    assert history.find_candidates("", "", [], [entry], threshold=0.3) == []


def test_find_candidates_sorted_most_recent_day_first():
    older = history.HistoryEntry(
        slug="fed-a", headline="Fed weighs a rate cut", subject="", domain="markets",
        importance=3, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day="2026-08-05")
    newer = history.HistoryEntry(
        slug="fed-b", headline="Fed signals a rate cut", subject="", domain="markets",
        importance=3, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day="2026-08-07")
    candidates = history.find_candidates(
        "Fed cuts rates", "", [], [older, newer], threshold=0.2)
    assert [c.entry.slug for c in candidates] == ["fed-b", "fed-a"]


def test_entities_from_articles_dedupes_preserving_order():
    class FakeArticle:
        def __init__(self, matched):
            self.matched = matched

    arts = [FakeArticle(["Apple", "Nvidia"]), FakeArticle(["Nvidia", "Amazon"])]
    assert history.entities_from_articles(arts) == ["Apple", "Nvidia", "Amazon"]


def test_entities_from_articles_handles_no_matched_attr():
    class Bare:
        pass

    assert history.entities_from_articles([Bare()]) == []


def _candidate(day, importance):
    entry = history.HistoryEntry(
        slug="x", headline="Fed cuts rates", subject="", domain="markets",
        importance=importance, breaking=False, sentiment="neutral", summary="",
        article_count=1, entities=[], day=day)
    return history.Candidate(entry=entry, score=0.5)


def test_ground_truth_no_candidates():
    g = history.ground_truth(4, [])
    assert g == {"days_running": 1, "first_seen": None, "expected_trend": "steady"}


def test_ground_truth_days_running_counts_distinct_days_plus_today():
    candidates = [_candidate("2026-08-06", 2), _candidate("2026-08-07", 3),
                  _candidate("2026-08-08", 3)]
    g = history.ground_truth(4, candidates)
    assert g["days_running"] == 4          # 3 distinct past days + today
    assert g["first_seen"] == "2026-08-06"


def test_ground_truth_expected_trend_rising():
    g = history.ground_truth(5, [_candidate("2026-08-06", 2)])
    assert g["expected_trend"] == "rising"


def test_ground_truth_expected_trend_falling():
    g = history.ground_truth(2, [_candidate("2026-08-06", 5)])
    assert g["expected_trend"] == "falling"


def test_ground_truth_expected_trend_steady():
    g = history.ground_truth(3, [_candidate("2026-08-06", 3)])
    assert g["expected_trend"] == "steady"


def test_ground_truth_uses_earliest_candidate_for_delta():
    # Earliest (by day) candidate's importance is the baseline, not the latest.
    candidates = [_candidate("2026-08-06", 1), _candidate("2026-08-08", 5)]
    g = history.ground_truth(5, candidates)
    assert g["expected_trend"] == "rising"   # 5 - 1(earliest) > 0


def test_trend_matches():
    ground = {"days_running": 2, "first_seen": "2026-08-08", "expected_trend": "rising"}
    assert history.trend_matches("rising", ground) is True
    assert history.trend_matches("falling", ground) is False
    assert history.trend_matches("steady", ground) is False
