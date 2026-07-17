"""Phase 2 — the gemma sequence.

Four single-responsibility passes, each a separate ollama call with its own
system prompt and narrow input (deliberately not one mega-prompt — a 4B model is
far more reliable decomposed). They run STRICTLY SEQUENTIALLY so only one model
is resident at a time (16 GB GPU):

    triage      cluster the day's articles into stories, tag domain/importance/breaking
    planner     per story, choose which panels + data + chart shapes to render
    summarizer  per story, write subject + headline + tight summary
    layout      once per run, size every built panel on the 12-column grid

`enrich()` orchestrates the first three into a validated DayPlan; `layout()` runs
last, from run.py, once the real panels exist. Each pass' model is a one-line
config swap (config `models:`).

Design note — the planner is offered a MENU, not a vocabulary. `story_offers()`
computes which data actually exists for a story (is there a photo? enough outlets
to chart? a ticker the news is moving?) and the prompt lists only those. A 4B
model asked to invent panel keys hallucinates; the same model asked to pick from
six real lines does well. Anything it emits anyway is dropped in validation.
"""

from __future__ import annotations

import json
from datetime import date

import requests

from .enrichers import coverage as _coverage
from .fetch import Article
from .plan_schema import DayPlan, StorySpec

# ── ollama ────────────────────────────────────────────────────────────────────


class Ollama:
    def __init__(self, host: str, timeout: int = 180):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def chat_json(self, model: str, system: str, user: str, temperature: float = 0.2) -> dict:
        """One turn, forced JSON. Retries once on unparseable output."""
        for attempt in range(2):
            resp = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": temperature if attempt == 0 else 0.0},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
        return {}


# ── pre-ranking (cheap heuristic before the small model sees anything) ─────────


def rank_articles(articles: list[Article], limit: int) -> list[Article]:
    """Score by topic weight × recency, with a watchlist bump, and take the top
    `limit` so triage gets a manageable, already-biased candidate set."""
    n = len(articles) or 1

    def score(idx_art) -> float:
        idx, a = idx_art
        recency = 1.0 - (idx / n)                 # articles arrive freshest-first
        return a.weight * (0.5 + 0.5 * recency) + (0.6 * len(a.matched))

    ranked = sorted(enumerate(articles), key=score, reverse=True)
    return [a for _, a in ranked[:limit]]


# ── pass 1: triage ─────────────────────────────────────────────────────────────

_TRIAGE_SYS = (
    "You are a news editor. Group the numbered articles into distinct STORIES "
    "(merge articles about the same event). For each story pick a short slug, a "
    "headline, a domain (one of: politics, sports, tech, ai, markets, business, "
    "world, general), an importance 1-5 (5 = lead story), a boolean `breaking` "
    "(true only if this BROKE or developed materially in the last day — a new "
    "event, ruling, result, deal or announcement; false for ongoing/analysis "
    "coverage), and the list of article numbers it covers. Return ONLY JSON: "
    '{"stories":[{"slug","headline","domain","importance","breaking",'
    '"articles":[int,...]}]}. '
    "Return at most {top} stories, most important first."
)


def triage(ollama: Ollama, model: str, articles: list[Article], top: int) -> list[dict]:
    lines = []
    for i, a in enumerate(articles):
        tag = f" (watchlist: {', '.join(a.matched)})" if a.matched else ""
        lines.append(f"{i}. [{a.topic}] {a.title} — {a.source}{tag}")
    out = ollama.chat_json(
        model,
        _TRIAGE_SYS.replace("{top}", str(top)),
        "Articles:\n" + "\n".join(lines),
    )
    stories = out.get("stories", []) if isinstance(out, dict) else []
    return stories[:top]


# ── pass 2: planner (the "alive" decision) ─────────────────────────────────────

_PLANNER_SYS = (
    "You are a dashboard designer choosing how to render ONE news story.\n"
    "Every story ALREADY gets a written summary with its headlines — never plan "
    "that. You choose the EXTRA panels that make the story worth looking at.\n\n"
    "Rules:\n"
    "1. You may ONLY use keys from the AVAILABLE PANELS list. Never invent a key, "
    "a ticker, or a data source. If the list is empty, return {\"panels\":[]}.\n"
    "2. Pick 0-3 panels. Choose the ones that genuinely illuminate THIS story — "
    "a photo makes a big human story land; a price chart only matters if the news "
    "moves the stock; coverage panels show how hard a story is being reported. "
    "Quality over quantity: a weak panel is worse than no panel.\n"
    "3. `title` must be specific and human ('Nvidia's day', 'Who's covering this') "
    "— never 'Chart' or 'Panel'.\n"
    "4. For charts pick the shape that fits: line = a value over time, bar = "
    "comparing categories, pie = parts of a whole, scatter = correlation.\n"
    'Return ONLY JSON: {"panels":[{"type","title","data","chart_type"}]} where '
    "`type` is chart|metric|table|image, `data` is copied EXACTLY from the list "
    "(omit `data` for image), and `chart_type` is set only for charts."
)


def story_offers(story: dict, arts: list[Article], story_tickers: dict[str, str],
                 has_image: bool) -> list[tuple[str, str]]:
    """The real menu for one story: (data key, human description) for every panel
    whose data we can actually produce right now. Computed in code — never by the
    model — so the planner can only pick things that will really render."""
    offers: list[tuple[str, str]] = []

    if has_image:
        src = next((a.source for a in arts if getattr(a, "image_url", "")), "a wire")
        offers.append(("image", f"type=image — the story's news photo (from {src})"))

    for mode in _coverage.available(arts):
        if mode == "sources":
            n = len({a.source for a in arts})
            offers.append(("coverage:sources",
                           f"type=chart|table — how many articles each of the {n} "
                           f"outlets ran on this (bar)"))
        elif mode == "timeline":
            offers.append(("coverage:timeline",
                           "type=chart — when the articles landed, hour by hour "
                           "(bar; shows a story breaking)"))

    for name, tk in story_tickers.items():
        offers.append((f"stock:{tk}:1d",
                       f"type=chart — {name} ({tk}) share price through today (line)"))
        offers.append((f"stock:{tk}:trend",
                       f"type=chart — {name} ({tk}) % change over day / week / month (bar)"))
        offers.append((f"stock:{tk}",
                       f"type=metric — {name} ({tk}) latest price + day change"))
    return offers


def plan_story(ollama: Ollama, model: str, story: dict, arts: list[Article],
               offers: list[tuple[str, str]]) -> list[dict]:
    if not offers:
        return []
    heads = "\n".join(f"- {a.title} ({a.source})" for a in arts[:8])
    menu = "\n".join(f"  {key:22} {desc}" for key, desc in offers)
    user = (
        f"Story: {story.get('headline')}\n"
        f"Domain: {story.get('domain')}\n"
        f"Importance: {story.get('importance')}/5\n"
        f"Breaking: {'yes' if story.get('breaking') else 'no'}\n\n"
        f"Article headlines:\n{heads}\n\n"
        f"AVAILABLE PANELS (use these keys verbatim):\n{menu}\n\n"
        f"Design this story's panels."
    )
    out = ollama.chat_json(model, _PLANNER_SYS, user, temperature=0.4)
    panels = out.get("panels", []) if isinstance(out, dict) else []

    # Hard gate: the model may only use keys we actually offered. It sometimes
    # returns `data` as a list of keys instead of one string — that's a legible
    # intent, so fan it out into one panel per key rather than dropping it.
    valid = {k for k, _ in offers}
    kept: list[dict] = []
    for p in panels:
        if not isinstance(p, dict):
            continue
        if str(p.get("type", "")).lower() == "image":
            if "image" in valid:
                kept.append(p)
            continue
        raw = p.get("data")
        keys = raw if isinstance(raw, list) else [raw]
        for k in keys:
            if isinstance(k, str) and k in valid:
                kept.append({**p, "data": k})
    return kept[:3]


# ── pass 3: summarizer ─────────────────────────────────────────────────────────

_SUMMARY_SYS = (
    "Summarize this news story in 2-3 plain sentences for a personal dashboard. "
    "Be factual and neutral; no preamble. Also provide:\n"
    "- headline: a tightened one-line headline.\n"
    "- subject: a SHORT theme of 2-5 words naming the larger topic (e.g. "
    "'Apple–OpenAI Lawsuit', 'Warriors Coaching Change', 'US Crypto Legislation'). "
    "This is used as the section title, so make it a clean noun phrase, not a sentence.\n"
    'Return ONLY JSON: {"subject","headline","summary"}.'
)


def summarize_story(ollama: Ollama, model: str, story: dict, arts: list[Article]) -> dict:
    heads = "\n".join(f"- {a.title}: {a.summary[:200]}" for a in arts[:6])
    out = ollama.chat_json(
        model, _SUMMARY_SYS,
        f"Headline: {story.get('headline')}\nSources:\n{heads}",
        temperature=0.3,
    )
    return out if isinstance(out, dict) else {}


# ── pass 4: layout ─────────────────────────────────────────────────────────────
# Sizing used to be a formula in run.py. It's a judgement call — how much room a
# story deserves depends on what it *is*, not just how many headlines it has — so
# the model makes it. The model returns sizes only; run.py packs them into
# non-overlapping (x, y). That split is deliberate: a 4B model choosing raw grid
# coordinates produces overlaps, but choosing "this is a 6×10 panel" it does well.

_LAYOUT_SYS = (
    "You are laying out a news dashboard on a 12-column grid. For EVERY panel "
    "listed, return a width `w` (columns, 1-12) and height `h` (rows).\n\n"
    "Scale:\n"
    "- The grid is 12 columns wide. One row is ~30px; a screen shows ~24 rows.\n"
    "- w=12 is full width, w=6 half, w=4 a third, w=3 a quarter.\n"
    "- Panels flow left→right and wrap to a new line when a row exceeds 12 "
    "columns, so widths that sum to 12 sit side by side.\n\n"
    "Guidance:\n"
    "- markdown: needs ~3 rows for the summary plus ~1 per headline. Never < 5.\n"
    "- chart: unreadable below w=4 or h=6. Give real charts h=8-10.\n"
    "- metric: a single number. Small — around w=3, h=4. Never taller than 5.\n"
    "- image: a photo. Wants w=4-6 and h=7-10; too short and it crops badly.\n"
    "- table: ~2 rows of header plus 1 per data row.\n"
    "- Give the lead story (importance 5) more room than a minor one.\n"
    "- Pair a story's markdown with its visual on the SAME line (e.g. 6 + 6) so "
    "they read together, rather than stacking every panel full width.\n\n"
    'Return ONLY JSON: {"panels":[{"id":<int>,"w":<int>,"h":<int>}]} — one entry '
    "for every id given, no extras."
)


def layout(ollama: Ollama, model: str, manifest: list[dict]) -> dict[int, tuple[int, int]]:
    """Ask the model to size each panel. Returns {id: (w, h)}; ids the model
    omits or mangles simply fall back to the caller's defaults."""
    if not manifest:
        return {}
    lines = []
    for m in manifest:
        bits = [f"{m['id']}. type={m['kind']}"]
        if m.get("chart_type"):
            bits.append(f"({m['chart_type']})")
        bits.append(f'"{m["title"]}"')
        bits.append(f"importance={m['importance']}/5")
        if m.get("note"):
            bits.append(f"— {m['note']}")
        lines.append(" ".join(bits))
    out = ollama.chat_json(
        model, _LAYOUT_SYS,
        "Panels, in the order they will appear:\n" + "\n".join(lines),
        temperature=0.3,
    )
    sizes: dict[int, tuple[int, int]] = {}
    for p in (out.get("panels", []) if isinstance(out, dict) else []):
        if not isinstance(p, dict):
            continue
        pid, w, h = _int(p.get("id")), _int(p.get("w")), _int(p.get("h"))
        if pid is None or w is None or h is None:
            continue
        sizes[pid] = (max(1, min(12, w)), max(2, min(24, h)))
    return sizes


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── orchestration ──────────────────────────────────────────────────────────────


# Only these domains get stock panels at all — a political/world/sports story
# stays headlines + summary even if it name-drops a public company.
STOCK_DOMAINS = {"markets", "business", "tech", "ai"}


def _central_tickers(headline: str, arts: list[Article], config: dict) -> dict[str, str]:
    """Public companies *central* to a story → ticker. Centrality = a keyword in
    the story headline or an article TITLE (not the loose title+summary match used
    for ranking), so incidental mentions don't spawn charts."""
    hay = (headline + " " + " ".join(a.title for a in arts)).lower()
    out = {}
    for c in config.get("watchlist", {}).get("companies", []):
        tk = c.get("ticker")
        if not tk or tk.lower() == "private":
            continue
        if any(kw.lower() in hay for kw in c.get("keywords", [])):
            out[c["name"]] = tk
    return out


def _wants_stock(story: dict, config: dict) -> bool:
    """Stocks are for news that MOVES a stock, not a daily ticker readout. A
    company's chart shows up when the story broke today or is a lead item —
    otherwise its price is just noise you didn't ask for."""
    rules = config.get("stocks", {})
    if str(story.get("domain", "")).lower() not in STOCK_DOMAINS:
        return False
    if rules.get("breaking_only", True) and not story.get("breaking"):
        return int(story.get("importance") or 0) >= int(rules.get("min_importance", 5))
    return True


def enrich(articles: list[Article], config: dict, run_day: date | None = None) -> DayPlan:
    """Run the first three passes and return a validated DayPlan."""
    defaults = config.get("defaults", {})
    oc = config.get("ollama", {})
    models = config.get("models", {})
    ollama = Ollama(oc.get("host", "http://localhost:11434"),
                    oc.get("timeout_seconds", 180))

    candidates = rank_articles(articles, defaults.get("triage_candidates", 80))
    stories = triage(ollama, models.get("triage", "gemma4:e4b"),
                     candidates, defaults.get("top_stories", 8))

    plan = DayPlan(day=run_day or date.today())
    for st in stories:
        idxs = [i for i in st.get("articles", []) if isinstance(i, int) and 0 <= i < len(candidates)]
        arts = [candidates[i] for i in idxs] or candidates[:3]

        tickers = (_central_tickers(st.get("headline", ""), arts, config)
                   if _wants_stock(st, config) else {})
        has_image = any(getattr(a, "image_url", "") for a in arts)
        offers = story_offers(st, arts, tickers, has_image)

        panels = plan_story(ollama, models.get("planner", "gemma4:e4b"), st, arts, offers)
        summ = summarize_story(ollama, models.get("summarizer", "gemma4:e4b"), st, arts)

        raw = {
            "slug": st.get("slug"),
            "story": st.get("headline"),
            "headline": summ.get("headline") or st.get("headline"),
            "subject": summ.get("subject", ""),
            "domain": st.get("domain"),
            "importance": st.get("importance"),
            "breaking": st.get("breaking"),
            "summary": summ.get("summary", ""),
            "article_urls": [a.url for a in arts],
            "panels": panels,
        }
        spec = StorySpec.from_dict(raw)
        if spec:
            # keep the clustered articles on the spec — the coverage enricher and
            # hero_image() read them straight off it.
            spec._articles = arts  # type: ignore[attr-defined]
            plan.stories.append(spec)
    return plan
