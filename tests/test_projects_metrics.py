from datetime import datetime, timezone

from news.projects import metrics


# ── cycle_time_days ──────────────────────────────────────────────────────────

def test_cycle_time_days_computes_whole_and_fractional_days():
    result = metrics.cycle_time_days("2026-08-01T00:00:00.000Z", "2026-08-05T12:00:00.000Z")
    assert result == 4.5


def test_cycle_time_days_none_when_started_at_missing():
    assert metrics.cycle_time_days(None, "2026-08-05T00:00:00.000Z") is None


def test_cycle_time_days_none_when_completed_at_missing():
    assert metrics.cycle_time_days("2026-08-01T00:00:00.000Z", None) is None


# ── age_days ──────────────────────────────────────────────────────────────────

def test_age_days_computes_days_since_creation():
    now = datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc)
    result = metrics.age_days("2026-08-08T00:00:00.000Z", now)
    assert result == 10.0


def test_age_days_none_when_created_at_missing():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    assert metrics.age_days(None, now) is None


# ── completed_csv ─────────────────────────────────────────────────────────────

def test_completed_csv_header_and_rows():
    issues = [
        {"identifier": "HEL-1", "title": "Fix the thing", "startedAt": "2026-08-01T00:00:00.000Z",
         "completedAt": "2026-08-03T00:00:00.000Z"},
        {"identifier": "HEL-2", "title": "Ship the other thing", "startedAt": None,
         "completedAt": "2026-08-04T00:00:00.000Z"},
    ]
    csv_text = metrics.completed_csv(issues)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "id,title,completedAt,cycleTimeDays"
    assert lines[1] == "HEL-1,Fix the thing,2026-08-03T00:00:00.000Z,2.0"
    assert lines[2] == "HEL-2,Ship the other thing,2026-08-04T00:00:00.000Z,"


# ── open_csv ──────────────────────────────────────────────────────────────────

def test_open_csv_header_rows_and_bug_label_detection():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    issues = [
        {"identifier": "HEL-3", "title": "Old bug", "priority": 2,
         "createdAt": "2026-08-08T00:00:00.000Z", "labels": {"nodes": [{"name": "Bug"}]}},
        {"identifier": "HEL-4", "title": "Feature request", "priority": 3,
         "createdAt": "2026-08-16T00:00:00.000Z", "labels": {"nodes": [{"name": "Enhancement"}]}},
    ]
    csv_text = metrics.open_csv(issues, now)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "id,title,priority,isBug,createdAt,ageDays"
    assert lines[1] == "HEL-3,Old bug,2,true,2026-08-08T00:00:00.000Z,10.0"
    assert lines[2] == "HEL-4,Feature request,3,false,2026-08-16T00:00:00.000Z,2.0"
