"""FRED (St. Louis Fed) economic series — CPI/inflation, gas, unemployment, etc.

Needs a free API key in `FRED_API_KEY` (environment or the gitignored `.env`,
same place as HELIO_PAT — never in config or code). If the key is absent, fetch
returns None and the panel is simply dropped, so the pipeline runs fine without
it; FRED series just won't be offered until a key is set.

Series ids are stable FRED codes (CPIAUCSL = CPI, GASREGW = regular gas price,
UNRATE = unemployment rate, MORTGAGE30US = 30-yr mortgage, APU0000708111 = eggs).
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from . import Series

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_API = "https://api.stlouisfed.org/fred/series/observations"
_warned = False


def _api_key() -> str | None:
    """FRED_API_KEY from the environment, falling back to ./.env (gitignored)."""
    key = os.environ.get("FRED_API_KEY")
    if key:
        return key.strip()
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("FRED_API_KEY=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fetch(series_id: str, start: str = "2019-01-01") -> Series | None:
    global _warned
    series_id = (series_id or "").strip()
    if not series_id:
        return None
    key = _api_key()
    if not key:
        if not _warned:
            import sys
            print("· FRED_API_KEY not set — FRED series skipped (add it to .env "
                  "to enable them)", file=sys.stderr)
            _warned = True
        return None
    resp = requests.get(_API, params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "observation_start": start,
    }, timeout=30)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    points: list[tuple[str, float]] = []
    for o in obs:
        v = o.get("value")
        if v in (".", "", None):     # FRED marks missing observations "."
            continue
        try:
            points.append((o["date"], float(v)))
        except (KeyError, TypeError, ValueError):
            continue
    if len(points) < 2:
        return None
    return Series(
        points=points,
        label=series_id,
        source="FRED",
        url=f"https://fred.stlouisfed.org/series/{series_id}",
    )
