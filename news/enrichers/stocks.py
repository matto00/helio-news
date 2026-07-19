"""stock:<TICKER>[:<range>] — price data via yfinance (Yahoo, no API key).

Stocks are NOT a daily fixture — a ticker only earns a panel when the day's news
is actually moving it (see `agents.STOCK_*`). When it does, the planner picks the
horizon that fits the story:

  stock:TICKER:1d     intraday, 5-minute bars      → "what today's news did"
  stock:TICKER:1w     ~1 week of daily closes
  stock:TICKER:1mo    ~1 month of daily closes     (default)
  stock:TICKER:trend  day / week / month % change  → bar chart, the whole arc
  stock:TICKER        latest price + day change    (metric)

yfinance handles Yahoo's cookie/crumb auth + backoff, which raw HTTP to Yahoo or
stooq no longer survives headlessly. Returns None (panel dropped) on any failure
or a private/blank ticker, so a bad symbol never breaks the run.
"""

from __future__ import annotations

# range key → (yfinance period, interval, x-axis label, how many points to keep)
RANGES = {
    "1d":  ("1d",  "5m",  "time",  78),
    "1w":  ("5d",  "1d",  "date",  5),
    "1mo": ("2mo", "1d",  "date",  30),
}
DEFAULT_RANGE = "1mo"

# Sessions back for each trend bucket (~5 trading days a week, ~21 a month).
TREND_BUCKETS = (("Day", 1), ("Week", 5), ("Month", 21))


def _history(ticker: str, period: str, interval: str):
    import yfinance as yf

    return yf.Ticker(ticker).history(period=period, interval=interval)


def _series(ticker: str, rng: str) -> list[tuple[str, float]]:
    period, interval, _, keep = RANGES[rng]
    df = _history(ticker, period, interval)
    fmt = "%H:%M" if interval.endswith("m") else "%Y-%m-%d"
    out: list[tuple[str, float]] = []
    for idx, close in zip(df.index, df["Close"]):
        if close == close:  # drop NaN
            out.append((idx.strftime(fmt), float(close)))
    return out[-keep:]


def _closes(ticker: str) -> list[float]:
    df = _history(ticker, "3mo", "1d")
    return [float(c) for c in df["Close"] if c == c]


def _pct(now: float, then: float) -> float:
    return round((now - then) / then * 100, 2) if then else 0.0


def build(arg, panel, story):
    from . import SourceData, T_STR, T_NUM

    # arg is "TICKER" or "TICKER:range" (resolve() only splits off the prefix).
    ticker, _, rng = (arg or "").strip().partition(":")
    ticker = ticker.upper()
    rng = rng.lower() or (DEFAULT_RANGE if panel.type == "chart" else "")
    if not ticker or ticker == "PRIVATE":
        return None

    # ── trend: day/week/month % change as a bar chart ────────────────────────
    if rng == "trend":
        closes = _closes(ticker)
        if len(closes) < 2:
            return None
        latest = closes[-1]
        rows = []
        for label, back in TREND_BUCKETS:
            if len(closes) > back:
                rows.append([label, _pct(latest, closes[-1 - back])])
        if not rows:
            return None
        return SourceData(
            key=f"stock-{ticker}-trend",
            columns=[{"name": "period", "type": T_STR},
                     {"name": "change_pct", "type": T_NUM}],
            rows=rows,
            mapping={"xAxis": "period", "yAxis": "change_pct"},
            panel_type="chart",
            chart_type="bar",
        )

    # ── metric: latest price + day change ────────────────────────────────────
    if panel.type == "metric":
        closes = _closes(ticker)
        if len(closes) < 2:
            return None
        latest, prev = closes[-1], closes[-2]
        return SourceData(
            key=f"stock-{ticker}-metric",
            columns=[{"name": "label", "type": T_STR},
                     {"name": "price", "type": T_NUM},
                     {"name": "change_pct", "type": T_NUM}],
            rows=[[ticker, round(latest, 2), _pct(latest, prev)]],
            mapping={"value": "price", "label": "label", "unit": "USD"},
            panel_type="metric",
        )

    # ── chart: a price line over the chosen window ───────────────────────────
    if rng not in RANGES:
        rng = DEFAULT_RANGE
    series = _series(ticker, rng)
    if len(series) < 2:
        # Intraday is empty on weekends/holidays — fall back rather than drop.
        if rng == "1d":
            rng = "1mo"
            series = _series(ticker, rng)
        if len(series) < 2:
            return None
    xlabel = RANGES[rng][2]
    return SourceData(
        key=f"stock-{ticker}-{rng}",
        columns=[{"name": xlabel, "type": T_STR},
                 {"name": "close", "type": T_NUM}],
        rows=[[d, round(c, 2)] for d, c in series],
        mapping={"xAxis": xlabel, "yAxis": "close"},
        panel_type="chart",
        chart_type="line",
    )
