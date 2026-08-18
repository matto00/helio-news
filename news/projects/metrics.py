"""Pure per-row math for project-pulse CSVs. Everything a helio pipeline can
compute (averages, counts, week-bucketing, sorting) stays out of here — this
module exists only because helio's `compute` step cannot do date arithmetic
on CSV-sourced values (verified live; see
docs/superpowers/specs/2026-08-18-project-pulse-design.md). cycleTimeDays and
ageDays are the one per-row exception; every aggregate statistic downstream
is computed by a helio pipeline, not here."""

from __future__ import annotations

import csv
import io
from datetime import datetime


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def cycle_time_days(started_at: str | None, completed_at: str | None) -> float | None:
    started, completed = _parse(started_at), _parse(completed_at)
    if started is None or completed is None:
        return None
    return round((completed - started).total_seconds() / 86400, 2)


def age_days(created_at: str | None, now: datetime) -> float | None:
    created = _parse(created_at)
    if created is None:
        return None
    return round((now - created).total_seconds() / 86400, 2)


def completed_csv(issues: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "title", "completedAt", "cycleTimeDays"])
    for issue in issues:
        ct = cycle_time_days(issue.get("startedAt"), issue.get("completedAt"))
        writer.writerow([issue["identifier"], issue["title"], issue.get("completedAt", ""),
                         "" if ct is None else ct])
    return buf.getvalue()


def open_csv(issues: list[dict], now: datetime) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "title", "priority", "isBug", "createdAt", "ageDays"])
    for issue in issues:
        is_bug = any(label.get("name") == "Bug"
                     for label in issue.get("labels", {}).get("nodes", []))
        ad = age_days(issue.get("createdAt"), now)
        writer.writerow([issue["identifier"], issue["title"], issue.get("priority", 0),
                         "true" if is_bug else "false", issue.get("createdAt", ""),
                         "" if ad is None else ad])
    return buf.getvalue()
