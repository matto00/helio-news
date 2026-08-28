"""Linear GraphQL client for project-pulse — LINEAR_API_KEY-gated, fail-soft
on a missing key (matches fred.py/yahoo.py). This is a DIRECT HTTP client,
not the mcp__linear__* tools: those only exist inside an interactive Claude
session with the Linear MCP server configured, not for this unattended daily
script (news.run, launched by systemd with no MCP/Claude involved).

Unlike fred.py/yahoo.py, network/GraphQL failures here are NOT swallowed —
they propagate so news/projects/build.py's per-project try/except can log
and skip just that project, per the design spec's fail-soft-per-project
(not fail-soft-inside-the-client) philosophy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

_API = "https://api.linear.app/graphql"
# `first:` is a PAGE size, not a result limit — Linear caps a single page at
# 250. Walk `pageInfo.endCursor` to get the whole set; without this a team with
# more than one page had its oldest tickets silently dropped, quietly skewing
# every cycle-time and backlog-age number on its project-pulse board.
_PAGE_SIZE = 250
# Belt-and-braces bound on the walk, so a pathological cursor can't spin the
# daily run forever. 40 pages = 10k tickets, far beyond any real team here.
MAX_PAGES = 40
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_warned = False

_COMPLETED_QUERY = """
query($teamName: String!, $since: DateTimeOrDuration!, $after: String) {
  issues(
    filter: { team: { name: { eq: $teamName } }, completedAt: { gte: $since } }
    first: 250
    after: $after
    orderBy: updatedAt
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      priority
      createdAt
      startedAt
      completedAt
      labels { nodes { name } }
    }
  }
}
"""

_OPEN_QUERY = """
query($teamName: String!, $after: String) {
  issues(
    filter: { team: { name: { eq: $teamName } }, completedAt: { null: true }, canceledAt: { null: true } }
    first: 250
    after: $after
    orderBy: createdAt
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      priority
      createdAt
      labels { nodes { name } }
    }
  }
}
"""


def _api_key() -> str | None:
    """LINEAR_API_KEY from the environment, falling back to ./.env (gitignored)."""
    key = os.environ.get("LINEAR_API_KEY")
    if key:
        return key.strip()
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("LINEAR_API_KEY=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _query(query: str, variables: dict) -> list[dict] | None:
    global _warned
    key = _api_key()
    if not key:
        if not _warned:
            print("· LINEAR_API_KEY not set — project boards skipped (add it to .env "
                  "to enable them)", file=sys.stderr)
            _warned = True
        return None
    rows: list[dict] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        resp = requests.post(_API,
                             json={"query": query, "variables": {**variables, "after": cursor}},
                             headers={"Authorization": key, "Content-Type": "application/json"},
                             timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Linear API error: {data['errors']}")
        issues = data["data"]["issues"]
        rows.extend(issues["nodes"])
        page = issues.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return rows
        cursor = page.get("endCursor")
        if not cursor:
            # hasNextPage without a cursor: nothing to advance on, so stop
            # rather than re-request page one forever.
            return rows
    print(f"· Linear query for team={variables.get('teamName', '?')!r} stopped at the "
          f"{MAX_PAGES}-page budget ({len(rows)} tickets) — results may be truncated",
          file=sys.stderr)
    return rows


def fetch_completed(team_name: str, lookback_days: int) -> list[dict] | None:
    """Tickets completed within the last `lookback_days` for `team_name` —
    for velocity + cycle-time metrics."""
    return _query(_COMPLETED_QUERY, {"teamName": team_name, "since": f"-P{lookback_days}D"})


def fetch_open(team_name: str) -> list[dict] | None:
    """Every currently-open (not completed, not canceled) ticket for
    `team_name`, no date bound — a stale old bug must still surface as
    "oldest open" even if untouched in months."""
    return _query(_OPEN_QUERY, {"teamName": team_name})
