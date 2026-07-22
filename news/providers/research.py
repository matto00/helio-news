"""research — the long-tail data agent (Claude + web search), grounded.

When no configured `series:` adapter matches a story but the story is clearly
about a trackable quantity, this asks Claude (with the web-search server tool) to
find a REAL public series for it from an authoritative source. It is the
agentic, open-ended counterpart to the fixed provider adapters — and it is held
to the SAME honesty bar as the `facts` pass, two ways:

  1. the cited `source_url` must be on the authoritative-domain allowlist
     (config `research.domains`), and
  2. a verbatim `quote` the agent returns must be found in a fresh re-fetch of
     that source (the `agents._grounded` check the facts pass already uses) —
     so a fabricated series with an invented citation fails before it can render.

Off by default (`research.enabled: false`) and gated hard (lead/breaking stories,
a per-run budget), because each call is a real, billable Claude request with
network web search. Needs `anthropic` installed and `ANTHROPIC_API_KEY` in the
environment or `.env`; without either, it warns once and returns None, so the
pipeline runs unchanged until it's deliberately switched on.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import Series

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_MODEL_DEFAULT = "claude-opus-4-8"
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_MIN_POINTS = 3
_warned = False


_SYSTEM = (
    "You are a data researcher for a news dashboard. Given ONE news story, decide "
    "whether there is a REAL, PUBLIC, quantitative time series that would put the "
    "story in context (e.g. a story about inflation → the CPI series; about "
    "wildfires → acres burned per year; about a currency → its exchange rate). "
    "Use the web_search tool to find the actual series from an AUTHORITATIVE "
    "source — a government statistical agency, central bank, or a reputable public "
    "dataset (e.g. *.gov, Eurostat, OECD, World Bank, WHO, IMF, Our World in "
    "Data). Do NOT use blogs, news articles, or aggregators as the source.\n"
    "Return the figures EXACTLY as published — never estimate, interpolate, round, "
    "or invent a data point. If you cannot find a real series from an authoritative "
    "source, or the story has no meaningful quantitative series, return "
    '{"relevant": false}.\n'
    "When you do find one, return ONLY JSON:\n"
    '{"relevant": true, "label": "<2-5 word name of the quantity>", '
    '"unit": "<unit or empty>", "source_name": "<publisher, e.g. \'BLS\'>", '
    '"source_url": "<the exact page/dataset URL the numbers come from>", '
    '"quote": "<one verbatim sentence from that source stating a recent value — '
    "copied exactly, at least a full sentence>\", "
    '"points": [{"date": "YYYY-MM-DD", "value": <number>}, ...]} '
    "with the series in chronological order, at most 30 points. The quote must be "
    "text that actually appears on source_url."
)


def _api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        print(f"· research: {msg}", file=sys.stderr)
        _warned = True


def _domain_ok(url: str, allow: list[str]) -> bool:
    """True if the URL's host contains an allowlisted authoritative fragment."""
    m = re.match(r"https?://([^/]+)", str(url).strip(), re.I)
    if not m:
        return False
    host = m.group(1).lower()
    return any(str(frag).lower() in host for frag in allow)


def _parse_json(text: str) -> dict:
    """Pull the JSON object out of the model's final text (tolerant of prose)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def _clean_points(raw) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        d, v = str(p.get("date", "")).strip(), p.get("value")
        try:
            if d:
                out.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    return out[:30]


def _fetch_text(url: str) -> str:
    """Best-effort readable text of the cited source, for the grounding check."""
    try:
        import trafilatura

        html = trafilatura.fetch_url(url)
        if html:
            txt = trafilatura.extract(html) or ""
            if txt:
                return txt
            return html
    except Exception:
        pass
    try:
        import requests

        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        return r.text or ""
    except Exception:
        return ""


def research_series(story: dict, bodies: str, config: dict) -> Series | None:
    """Agentic search for a real contextual series → Series, or None.

    Returns None (drop the panel) on: disabled, no key/SDK, a non-relevant
    verdict, a non-allowlisted source, a quote that isn't found in the source, or
    any API/parse failure — so a bad result never reaches the board and never
    breaks a run."""
    rc = config.get("research", {})
    if not rc.get("enabled"):
        return None
    key = _api_key()
    if not key:
        _warn_once("ANTHROPIC_API_KEY not set — research disabled")
        return None
    try:
        import anthropic
    except ImportError:
        _warn_once("`anthropic` not installed — research disabled (pip install anthropic)")
        return None

    try:
        client = anthropic.Anthropic(api_key=key)
        model = rc.get("model", _MODEL_DEFAULT)
        user = (
            f"Story: {story.get('headline')}\n"
            f"Domain: {story.get('domain')}\n\n"
            f"Reporting:\n{(bodies or '')[:4000]}\n\n"
            "Find a real public data series that puts this story in context."
        )
        messages = [{"role": "user", "content": user}]
        resp = None
        for _ in range(4):                       # resume server-tool pauses
            resp = client.messages.create(
                model=model, max_tokens=4000, system=_SYSTEM,
                tools=[_WEB_SEARCH_TOOL], thinking={"type": "adaptive"},
                messages=messages,
            )
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        if resp is None:
            return None
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text")
    except Exception as e:                        # any API/network/tool failure
        _warn_once(f"call failed ({type(e).__name__}) — series dropped")
        return None

    data = _parse_json(text)
    if not isinstance(data, dict) or not data.get("relevant"):
        return None

    url = str(data.get("source_url", "")).strip()
    if not _domain_ok(url, rc.get("domains", [])):
        return None

    # Honesty gate: the agent's verbatim quote must really be on the cited page.
    from ..agents import _grounded, _norm_text     # local import: avoid cycle

    page = _fetch_text(url)
    if not page or not _grounded(str(data.get("quote", "")), _norm_text(page)):
        return None

    points = _clean_points(data.get("points"))
    if len(points) < _MIN_POINTS:
        return None

    return Series(
        points=points,
        label=str(data.get("label", "") or "Series")[:40],
        source=str(data.get("source_name", "") or "research")[:40],
        url=url,
        unit=str(data.get("unit", "") or "")[:20],
    )
