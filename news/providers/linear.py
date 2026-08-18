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
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_warned = False

_COMPLETED_QUERY = """
query($teamName: String!, $since: DateTimeOrDuration!) {
  issues(
    filter: { team: { name: { eq: $teamName } }, completedAt: { gte: $since } }
    first: 250
    orderBy: updatedAt
  ) {
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
query($teamName: String!) {
  issues(
    filter: { team: { name: { eq: $teamName } }, completedAt: { null: true }, canceledAt: { null: true } }
    first: 250
    orderBy: createdAt
  ) {
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
    resp = requests.post(_API, json={"query": query, "variables": variables},
                         headers={"Authorization": key, "Content-Type": "application/json"},
                         timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear API error: {data['errors']}")
    nodes = data["data"]["issues"]["nodes"]
    if len(nodes) == 250:
        print(f"· Linear query for team={variables.get('teamName', '?')!r} hit the 250-row "
              f"cap — results may be truncated (oldest tickets silently dropped)", file=sys.stderr)
    return nodes


def fetch_completed(team_name: str, lookback_days: int) -> list[dict] | None:
    """Tickets completed within the last `lookback_days` for `team_name` —
    for velocity + cycle-time metrics."""
    return _query(_COMPLETED_QUERY, {"teamName": team_name, "since": f"-P{lookback_days}D"})


def fetch_open(team_name: str) -> list[dict] | None:
    """Every currently-open (not completed, not canceled) ticket for
    `team_name`, no date bound — a stale old bug must still surface as
    "oldest open" even if untouched in months."""
    return _query(_OPEN_QUERY, {"teamName": team_name})
