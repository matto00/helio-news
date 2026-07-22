"""Real external data providers for the `series:` enricher.

Each provider exposes ``fetch(id, …) -> Series | None`` and returns a REAL,
citable time series — never model-authored numbers. A fetch that fails, is
gated (no API key), or comes back too thin returns None, so the panel is simply
dropped (the honesty invariant: nothing on the board that isn't a real source).

The `Series` a provider returns carries its own provenance (`source` + `url`),
which the enricher surfaces as the chart's source-attribution footnote.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Series:
    """One fetched time series plus its provenance."""

    points: list[tuple[str, float]]   # (date "YYYY-MM-DD", value), oldest→newest
    label: str                         # human name, e.g. "US CPI"
    source: str                        # attribution, e.g. "FRED" / "Yahoo Finance"
    url: str                           # citable source URL for this exact series
    unit: str = ""                     # optional display unit

    def attribution(self) -> str:
        """The footnote line a panel shows to credit this data."""
        return f"Source: {self.source}"
