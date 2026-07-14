"""stock:<TICKER> — price data via yfinance (Yahoo, no API key).

  chart  → date + close series (last ~30 sessions)   → line chart
  metric → single row: label, price, day change %     → KPI tile

yfinance handles Yahoo's cookie/crumb auth + backoff, which raw HTTP to Yahoo or
stooq no longer survives headlessly. Returns None (panel dropped) on any
failure or a private/blank ticker, so a bad symbol never breaks the run.
"""

from __future__ import annotations

SESSIONS = 30


def _daily_closes(ticker: str) -> list[tuple[str, float]]:
    import yfinance as yf

    df = yf.Ticker(ticker).history(period="2mo", interval="1d")
    out: list[tuple[str, float]] = []
    for idx, close in zip(df.index, df["Close"]):
        if close == close:  # drop NaN
            out.append((idx.strftime("%Y-%m-%d"), float(close)))
    return out[-SESSIONS:]


def build(arg, panel, story):
    from . import SourceData, T_STR, T_NUM

    ticker = (arg or "").strip().upper()
    if not ticker or ticker == "PRIVATE":
        return None
    series = _daily_closes(ticker)
    if len(series) < 2:
        return None

    if panel.type == "metric":
        latest, prev = series[-1][1], series[-2][1]
        change_pct = round((latest - prev) / prev * 100, 2) if prev else 0.0
        return SourceData(
            key=f"stock-{ticker}-metric",
            columns=[
                {"name": "label", "type": T_STR},
                {"name": "price", "type": T_NUM},
                {"name": "change_pct", "type": T_NUM},
            ],
            rows=[[ticker, round(latest, 2), change_pct]],
            mapping={"value": "price", "label": "label", "unit": "USD"},
            panel_type="metric",
        )

    return SourceData(
        key=f"stock-{ticker}-chart",
        columns=[
            {"name": "date", "type": T_STR},
            {"name": "close", "type": T_NUM},
        ],
        rows=[[d, round(c, 2)] for d, c in series],
        mapping={"xAxis": "date", "yAxis": "close"},
        panel_type="chart",
    )
