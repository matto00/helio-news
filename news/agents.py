"""Phase 2 — the gemma sequence.

Three single-responsibility passes, each a separate ollama call with its own
system prompt and narrow input (deliberately not one mega-prompt — a 4B model is
far more reliable decomposed). They run STRICTLY SEQUENTIALLY so only one model
is resident at a time (16 GB GPU):

    triage      cluster the day's articles into stories, tag domain + importance
    planner     per story, choose which panels + data the dashboard needs
    summarizer  per story, write headline + tight summary

`enrich()` orchestrates them into a validated DayPlan. Each pass' model is a
one-line config swap (config `models:`).
"""

from __future__ import annotations

import json
from datetime import date

import requests

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
    "world, general), an importance 1-5 (5 = lead story), and the list of article "
    "numbers it covers. Return ONLY JSON: "
    '{"stories":[{"slug","headline","domain","importance","articles":[int,...]}]}. '
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
    "You decide whether ONE news story warrants a STOCK visualization. Every story "
    "already gets a written summary and its headlines automatically — you only "
    "choose stock panels.\n"
    "Emit a chart and a metric ONLY when 'Public companies in this story' lists a "
    "TICKER and the story is genuinely about that company's business, product, "
    "market, earnings, deal or stock price. Otherwise return an empty list.\n"
    "  chart  data=\"stock:<TICKER>\"   → a price-history line chart\n"
    "  metric data=\"stock:<TICKER>\"   → latest price + day change\n"
    "Do NOT invent tickers; use only those provided. If none are listed, return "
    "{\"panels\":[]}.\n"
    'Return ONLY JSON: {"panels":[{"type":"chart"|"metric","title","data":"stock:TICKER"}]}.'
)


def plan_story(ollama: Ollama, model: str, story: dict, arts: list[Article],
               story_tickers: dict[str, str]) -> list[dict]:
    heads = "\n".join(f"- {a.title} ({a.source})" for a in arts[:8])
    if story_tickers:
        companies = "; ".join(f"{name} (TICKER {tk})" for name, tk in story_tickers.items())
    else:
        companies = "(none — no stock panels)"
    user = (
        f"Story: {story.get('headline')}\nDomain: {story.get('domain')}\n"
        f"Public companies in this story: {companies}\n"
        f"Article headlines:\n{heads}\n\nDesign the panels."
    )
    out = ollama.chat_json(model, _PLANNER_SYS, user, temperature=0.1)
    return out.get("panels", []) if isinstance(out, dict) else []


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


def enrich(articles: list[Article], config: dict, run_day: date | None = None) -> DayPlan:
    """Run the full sequence and return a validated DayPlan."""
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

        # Stock panels only for finance/tech domains AND companies central to the
        # story — so politics/world/sports stay headlines + summary.
        domain = str(st.get("domain", "general")).lower()
        story_tickers = (_central_tickers(st.get("headline", ""), arts, config)
                         if domain in STOCK_DOMAINS else {})
        panels = plan_story(ollama, models.get("planner", "gemma4:e4b"), st, arts, story_tickers)
        summ = summarize_story(ollama, models.get("summarizer", "gemma4:e4b"), st, arts)

        raw = {
            "slug": st.get("slug"),
            "story": st.get("headline"),
            "headline": summ.get("headline") or st.get("headline"),
            "subject": summ.get("subject", ""),
            "domain": st.get("domain"),
            "importance": st.get("importance"),
            "summary": summ.get("summary", ""),
            "article_urls": [a.url for a in arts],
            "panels": panels,
        }
        spec = StorySpec.from_dict(raw)
        if spec:
            # keep the clustered articles on the spec for the headlines enricher
            spec._articles = arts  # type: ignore[attr-defined]
            plan.stories.append(spec)
    return plan
