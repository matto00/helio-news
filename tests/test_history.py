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


def test_load_window_returns_no_files_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "does-not-exist")
    assert history.load_window(date(2026, 8, 9), lookback_days=7) == []
