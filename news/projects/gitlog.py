"""Recent commit subjects from a local repo's `main` branch — raw material
for the project-pulse narrative pass (news/projects/narrative.py). Not a
metrics source; commit volume is deliberately not a tracked panel (see the
design spec's non-goals)."""

from __future__ import annotations

import subprocess


def fetch_recent_subjects(repo_path: str, since_days: int) -> list[str]:
    """Subject lines from `main`, most-recent-first, committed within the
    last `since_days`. Empty list on any failure (bad path, not a repo, no
    `main` branch) — this is best-effort narrative fuel, never fatal."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "main", f"--since={since_days}.days.ago",
             "--pretty=format:%s"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]
