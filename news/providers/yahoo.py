"""Yahoo Finance series via yfinance — commodities, FX, indices, crypto, equities.

Keyless (yfinance handles Yahoo's cookie/crumb auth + backoff). This is the same
data source `enrichers/stocks.py` uses for equities, generalised to any Yahoo
symbol so the `series:` enricher can put "the price of oil" (CL=F), gold (GC=F),
wheat (ZW=F) or bitcoin (BTC-USD) in context. Returns None on any failure or a
too-thin history, so a bad symbol never breaks a run.
"""

from __future__ import annotations

from . import Series


def fetch(symbol: str, period: str = "1y", interval: str = "1d") -> Series | None:
    import yfinance as yf

    symbol = (symbol or "").strip().upper()
    if not symbol:
        return None
    df = yf.Ticker(symbol).history(period=period, interval=interval)
    if df is None or df.empty or "Close" not in df:
        return None
    points = [(idx.strftime("%Y-%m-%d"), float(c))
              for idx, c in zip(df.index, df["Close"]) if c == c]  # drop NaN
    if len(points) < 2:
        return None
    return Series(
        points=points,
        label=symbol,
        source="Yahoo Finance",
        url=f"https://finance.yahoo.com/quote/{symbol}",
    )
