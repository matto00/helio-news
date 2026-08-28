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
import re
import time
from contextlib import contextmanager
from datetime import date

import requests

# ── timing (where does the run's wall-clock actually go?) ──────────────────────
# A tiny accumulator so a run can report per-stage cost without a profiler. Each
# `with timed("extract"): …` adds to the stage's (call count, total seconds).

TIMINGS: dict[str, list] = {}


@contextmanager
def timed(label: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        d = TIMINGS.setdefault(label, [0, 0.0])
        d[0] += 1
        d[1] += time.perf_counter() - t0


def timings_report() -> str:
    """One-line-per-stage summary, slowest first: stage, calls, total, avg."""
    if not TIMINGS:
        return "· timings: (none recorded)"
    rows = sorted(TIMINGS.items(), key=lambda kv: -kv[1][1])
    total = sum(t for _, t in TIMINGS.values())
    lines = ["· timings — stage: calls, total, avg (slowest first):"]
    for label, (n, tot) in rows:
        lines.append(f"·   {label:11} {n:3} calls  {tot:7.1f}s  {tot / max(n, 1):6.1f}s avg")
    lines.append(f"·   {'TOTAL':11} {'':3}         {total:7.1f}s")
    return "\n".join(lines)

from . import history as _history
from .enrichers import coverage as _coverage
from .enrichers import history as _history_enricher
from .fetch import Article, hydrate_bodies
from .plan_schema import DayPlan, StorySpec

# ── ollama ────────────────────────────────────────────────────────────────────


class Ollama:
    def __init__(self, host: str, timeout: int = 180, num_ctx: int | None = None):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx

    def chat_json(self, model: str, system: str, user: str, temperature: float = 0.2,
                  think: str | None = None) -> dict:
        """One turn, forced JSON. Retries once on unparseable output.

        `think` is the gpt-oss reasoning effort (low|medium|high); the model's
        thinking is returned in a separate channel and discarded — we only parse
        the final JSON `content`. `num_ctx` (set on the client) is passed as an
        ollama option so long article bodies aren't silently truncated."""
        options: dict = {"num_ctx": self.num_ctx} if self.num_ctx else {}
        for attempt in range(2):
            options["temperature"] = temperature if attempt == 0 else 0.0
            payload: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": "json",
                "options": options,
            }
            if think:
                payload["think"] = think
            resp = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
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
    "You are a news editor. Group the numbered articles into distinct STORIES.\n"
    "A STORY is ONE specific event or development — a single ruling, result, deal, "
    "launch, incident, report or announcement — together with the articles that "
    "cover THAT event. Merge two articles only when they report the SAME event.\n"
    "Do NOT combine distinct events into one broad umbrella, even when they share a "
    "topic or domain. Keep them separate:\n"
    "- two different companies' earnings are two stories, not one 'Tech Earnings';\n"
    "- a new AI law and a new social-media law are two stories, not one 'Regulation';\n"
    "- one team's trade and another team's trade are two stories, not one 'Trades'.\n"
    "Each story's headline must name its ONE specific event — never a theme or "
    "roundup ('Netflix adds live boxing', not 'Streaming Strategy'; 'Bucks trade "
    "Giannis to Heat', not 'NBA Trades').\n"
    "For each story pick a short slug, that specific headline, a domain (one of: "
    "politics, sports, tech, ai, markets, business, world, general), an importance "
    "1-5 (5 = lead story), a boolean `breaking` (true only if it BROKE or developed "
    "materially in the last day — a new event, ruling, result, deal or "
    "announcement; false for ongoing/analysis coverage), and the list of article "
    "numbers it covers. If more than {top} distinct stories exist, keep the {top} "
    "MOST IMPORTANT and drop the rest — never merge unrelated events just to fit.\n"
    "{sections}"
    'Return ONLY JSON: {"stories":[{"slug","headline","domain","importance",'
    '"breaking","articles":[int,...]}]}, at most {top}, most important first.'
)


def domain_to_board(config: dict) -> dict[str, str]:
    """{story domain → section board name} from config `dashboards.sections`.
    Lives here rather than in run.py because both the triage quota and the
    board routing key off it, and they must never disagree."""
    sections = config.get("dashboards", {}).get("sections", {})
    out: dict[str, str] = {}
    for board, domains in sections.items():
        for d in domains or []:
            out[normalize_domain(d)] = board
    return out


def normalize_domain(value) -> str:
    """Triage's `domain` is free text from a model — casing and stray whitespace
    are routine. Normalize once, here, so a ' Sports ' never silently fails to
    match a configured section and drops the story off every board."""
    return str(value or "").strip().lower()


def _section_prompt(routing: dict[str, str], per_section: int) -> str:
    """The section-coverage instruction, or empty when no sections are configured."""
    if not routing or per_section <= 0:
        return ""
    boards: dict[str, list[str]] = {}
    for domain, board in routing.items():
        boards.setdefault(board, []).append(domain)
    listed = "; ".join(f"{board} ({'/'.join(sorted(domains))})"
                       for board, domains in boards.items())
    return (f"The day is published as these section boards: {listed}. "
            f"Cover the day BROADLY: include up to {per_section} of the strongest "
            f"stories for EACH section whenever the articles support one, before "
            f"spending remaining slots on additional stories from any section. "
            f"Never invent a story to fill a section — if the articles carry no "
            f"real sports story today, return none.\n")


def apply_section_quota(stories: list[dict], routing: dict[str, str],
                        per_section: int, top: int) -> list[dict]:
    """Reserve each section board up to `per_section` slots, then fill the rest
    by importance, up to `top` overall.

    The prompt asks for breadth; this enforces it. Triage ranked the day
    GLOBALLY before this existed, so a tech-heavy morning filled all 8 slots
    with tech and the Sports / Markets boards rendered empty. Judgement (which
    sports story is the strongest) stays with the model; the bookkeeping (that
    sports gets a slot at all) is code — the same split as assign_articles and
    the planner/layout passes.

    Unused quota is not wasted: a section with no stories gives its slots back
    to the fill round. A story whose domain routes to no board (`general`) is
    never reserved but still competes for fill slots, since the overview digest
    can use it."""
    if not stories:
        return []
    if per_section <= 0 or not routing:
        return sorted(stories, key=_importance, reverse=True)[:top]

    ranked = sorted(enumerate(stories), key=lambda p: (-_importance(p[1]), p[0]))
    reserved_by_board: dict[str, int] = {}
    chosen: list[tuple[int, dict]] = []
    leftovers: list[tuple[int, dict]] = []

    for idx, story in ranked:
        board = routing.get(normalize_domain(story.get("domain")))
        if board is not None and reserved_by_board.get(board, 0) < per_section \
                and len(chosen) < top:
            reserved_by_board[board] = reserved_by_board.get(board, 0) + 1
            chosen.append((idx, story))
        else:
            leftovers.append((idx, story))

    for pair in leftovers:
        if len(chosen) >= top:
            break
        chosen.append(pair)

    return [story for _, story in sorted(chosen, key=lambda p: (-_importance(p[1]), p[0]))]


def _importance(story: dict) -> float:
    try:
        return float(story.get("importance", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def triage(ollama: Ollama, model: str, articles: list[Article], top: int,
           think: str | None = None, routing: dict[str, str] | None = None,
           per_section: int = 0) -> list[dict]:
    lines = []
    for i, a in enumerate(articles):
        tag = f" (watchlist: {', '.join(a.matched)})" if a.matched else ""
        lines.append(f"{i}. [{a.topic}] {a.title} — {a.source}{tag}")
    routing = routing or {}
    out = ollama.chat_json(
        model,
        _TRIAGE_SYS.replace("{top}", str(top))
                   .replace("{sections}", _section_prompt(routing, per_section)),
        "Articles:\n" + "\n".join(lines),
        think=think,
    )
    stories = out.get("stories", []) if isinstance(out, dict) else []
    return apply_section_quota(stories, routing, per_section, top)


# ── article ↔ story membership (re-derived in code, not trusted from triage) ────
# Triage decides WHAT the day's stories are; it also returns which article numbers
# belong to each, but that index bookkeeping is fragile — over dozens of candidates
# a model will occasionally hand a story the wrong article number, and downstream
# passes then confabulate a whole story from a mismatched body. So we throw away
# triage's index list and re-derive membership here by content overlap: judgement
# to the model, bookkeeping to the code (the same split as planner/layout).

_STOPWORDS = frozenset((
    "the a an and or of to in on for with at by from as is are was were be been being "
    "this that these those it its his her their our your my i he she they we you but not "
    "has have had will would could should may might can do does did new says say said "
    "after over into out up down off than then now over amid vs"
).split())


def _tokens(text: str) -> set[str]:
    """Significant lowercase word tokens (drop stopwords and 1-2 char noise)."""
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower())
            if len(w) > 2 and w not in _STOPWORDS}


def assign_articles(stories: list[dict], candidates: list[Article]) -> dict[int, list[Article]]:
    """Assign each candidate article to the ONE triage story it best matches, by
    token overlap between the article and the story's headline plus a bonus for a
    shared watchlist entity. Returns {story_index: [articles, best match first]}.
    An article that matches nothing is left out; a story that ends up with no
    article is dropped by the caller rather than summarised from its headline."""
    story_toks = [_tokens(f"{s.get('headline','')} {str(s.get('slug','')).replace('-', ' ')}")
                  for s in stories]
    story_head = [(s.get("headline", "") or "").lower() for s in stories]
    buckets: dict[int, list[tuple[float, Article]]] = {i: [] for i in range(len(stories))}

    for a in candidates:
        atoks = _tokens(a.title) | _tokens((a.summary or "")[:200])
        best_i, best_score = -1, 0.0
        for i, stoks in enumerate(story_toks):
            score = float(len(atoks & stoks))
            score += 2.0 * sum(1 for ent in a.matched if ent.lower() in story_head[i])
            if score > best_score:
                best_i, best_score = i, score
        if best_i >= 0 and best_score >= 2:        # ≥2 shared tokens (or a shared entity):
            buckets[best_i].append((best_score, a))  # one weak token is how off-topic bodies
                                                     # leaked into generic-headline stories

    # Stable sort keeps triage's freshest-first order among equal-scoring articles.
    return {i: [a for _, a in sorted(b, key=lambda x: -x[0])] for i, b in buckets.items()}


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
                 has_image: bool, n_facts: int = 0,
                 series_specs: list[dict] | None = None,
                 research_label: str = "",
                 history_occurrences: int = 0) -> list[tuple[str, str]]:
    """The real menu for one story: (data key, human description) for every panel
    whose data we can actually produce right now. Computed in code — never by the
    model — so the planner can only pick things that will really render."""
    offers: list[tuple[str, str]] = []

    if has_image:
        src = next((a.source for a in arts if getattr(a, "image_url", "")), "a wire")
        offers.append(("image", f"type=image — the story's news photo (from {src})"))

    if n_facts >= MIN_NUMERIC_FACTS:
        offers.append(("facts:numbers",
                       f"type=table — the {n_facts} key figures in this story "
                       f"(amounts, counts, %) pulled and fact-checked from the "
                       f"reporting, shown as a grid of stat tiles"))

    if history_occurrences >= _history_enricher.MIN_OCCURRENCES:
        offers.append(("history:timeline",
                       f"type=table — this story's last {history_occurrences} days "
                       f"of coverage, a quick timeline of how it developed"))

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

    # Contextual data series: a real public dataset for a quantity this story is
    # about (inflation, gas, oil, …), fetched from FRED/Yahoo and put on a trend
    # line. Only offered when a configured series is central to the story.
    for spec in (series_specs or []):
        prov, sid = str(spec.get("provider", "")).lower(), str(spec.get("id", ""))
        if not prov or not sid:
            continue
        key = f"series:{prov}:{sid}"
        fidelity = str(spec.get("fidelity", "")).lower()
        if fidelity == "monthly":
            key += ":monthly"
        shape = "monthly average" if fidelity == "monthly" else "as reported"
        offers.append((key,
                       f"type=chart — {spec.get('name', sid)}: the real multi-year "
                       f"trend from {prov.upper()} ({shape}), to put this story in "
                       f"context (line)"))

    # Agent-researched series (the long-tail fallback): a real dataset found and
    # source-verified for this story when no configured series matched.
    if research_label:
        offers.append(("research:series",
                       f"type=chart — {research_label}: a data series researched "
                       f"from an authoritative public source to put this story in "
                       f"context (line)"))
    return offers


def plan_story(ollama: Ollama, model: str, story: dict, arts: list[Article],
               offers: list[tuple[str, str]], think: str | None = None) -> list[dict]:
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
    out = ollama.chat_json(model, _PLANNER_SYS, user, temperature=0.4, think=think)
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


def _article_text(a: Article, body_limit: int = 1500, teaser_limit: int = 220) -> str:
    """The best text we have for one article: its hydrated full body (trimmed) when
    we fetched one, else the RSS teaser. Lets the summarizer work off substance
    when it's available and degrade gracefully when it isn't."""
    body = (getattr(a, "body", "") or "").strip()
    if body:
        return body[:body_limit]
    return (a.summary or "").strip()[:teaser_limit]


def summarize_story(ollama: Ollama, model: str, story: dict, arts: list[Article],
                    think: str | None = None) -> dict:
    heads = "\n\n".join(f"- {a.title} ({a.source}):\n{_article_text(a)}" for a in arts[:6])
    out = ollama.chat_json(
        model, _SUMMARY_SYS,
        f"Headline: {story.get('headline')}\nSources:\n{heads}",
        temperature=0.3, think=think,
    )
    return out if isinstance(out, dict) else {}


# ── pass 3d: by-the-numbers (grounded extractor + adversarial critic) ────────────
# The pass that puts *substance* on the board instead of metadata. Two turns,
# both gpt-oss: an EXTRACTOR pulls the story's key figures out of the hydrated
# article bodies — each figure carried with the verbatim sentence it came from —
# then a CRITIC audits every candidate against that text. A figure survives only
# if (a) its quote is actually found in a body (checked in code, deterministically)
# AND (b) the critic agrees the quote states that value. Nothing invented, nothing
# rounded, everything traceable to a source sentence — the honesty invariant the
# rest of the dashboard already holds, now extended to real numbers.

# A story needs at least this many verified figures before a "By the numbers"
# table is worth a panel — one number is a sentence, not a table.
MIN_NUMERIC_FACTS = 2


def _bodies_text(arts: list[Article], per_article: int = 6000, total: int = 24000) -> str:
    """Concatenate the hydrated bodies for extraction/grounding. Only articles we
    actually fetched a body for contribute — teasers are too thin to mine and
    would just invite the model to guess."""
    chunks = []
    for a in arts:
        body = (getattr(a, "body", "") or "").strip()
        if body:
            chunks.append(f"[{a.source}] {a.title}\n{body[:per_article]}")
    return "\n\n---\n\n".join(chunks)[:total]


def _norm_text(s: str) -> str:
    """Fold to alnum+single-space lowercase so a quote can be matched against a
    body regardless of punctuation/curly-quote/whitespace differences."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s).lower())).strip()


def _grounded(quote: str, body_norm: str) -> bool:
    """True when `quote` is really present in the source text. Requires a solid
    run to match (not a couple of words), so a paraphrased or invented 'quote'
    fails here before it ever reaches the reader."""
    q = _norm_text(quote)
    if len(q) < 24:
        return False
    return q in body_norm or q[:80] in body_norm


_EXTRACT_NUMBERS_SYS = (
    "You are a data analyst pulling the KEY QUANTITATIVE FACTS out of a news story "
    "for a dashboard 'By the numbers' panel. Read the article text and extract the "
    "concrete figures that are CENTRAL to this story — money amounts, counts, "
    "percentages, scores, magnitudes, durations. For each figure return:\n"
    "- label: a 2-5 word noun phrase naming what the number measures "
    "('Deal value', 'Jobs cut', 'Vote margin', 'Quarterly revenue').\n"
    "- value: the figure EXACTLY as written in the text, keeping its unit or "
    "symbol ('$44 billion', '12,000', '47%', '3-1'). Never compute, round, "
    "convert, or infer a number — copy what the text says.\n"
    "- quote: the exact sentence from the text that states this figure, copied "
    "verbatim (one sentence, not a paraphrase).\n"
    "Skip trivia (publication dates, ages in passing, how many photos ran). If the "
    "text has no real figures, return an empty list. At most 6 facts, most "
    'important first. Return ONLY JSON {"facts":[{"label","value","quote"}]}.'
)


def extract_numbers(ollama: Ollama, model: str, story: dict, bodies: str,
                    think: str | None = None) -> list[dict]:
    if not bodies.strip():
        return []
    out = ollama.chat_json(
        model, _EXTRACT_NUMBERS_SYS,
        f"Story: {story.get('headline')}\n\nArticle text:\n{bodies}",
        temperature=0.2, think=think,
    )
    facts = out.get("facts", []) if isinstance(out, dict) else []
    return [f for f in facts if isinstance(f, dict)]


_CRITIC_NUMBERS_SYS = (
    "You are a fact-checker auditing figures a colleague extracted for a dashboard "
    "panel about ONE specific story. You are given that story's headline, the source "
    "text, and a list of candidate facts (each with a label, a value, and the quote "
    "it was drawn from). For EVERY candidate decide keep=true only if ALL hold:\n"
    "1. the figure is genuinely ABOUT THIS STORY as named by the headline — reject "
    "anything off-topic even if its quote is in the text, because the source may mix "
    "in unrelated material (e.g. a BASEBALL statistic has no place in a story about a "
    "BASKETBALL trade; an earnings-season number has no place in a story about one "
    "company's product);\n"
    "2. the quote appears in the source text and actually states that value;\n"
    "3. the label correctly describes what the number measures;\n"
    "4. the value was copied, not computed, rounded, or inferred;\n"
    "5. the figure is substantive (not trivia).\n"
    "Otherwise keep=false. Do not add new facts or edit values. Return ONLY JSON "
    '{"facts":[{"label","value","keep"}]} with one entry for every candidate, in '
    "the same order."
)


def critic_numbers(ollama: Ollama, model: str, story: dict, bodies: str,
                   candidates: list[dict], think: str | None = None) -> list[dict]:
    """Adversarial audit: return the subset of `candidates` the critic keeps.
    Matched back to the originals by order (and label as a fallback) so the
    verbatim quote survives even though the critic isn't asked to echo it."""
    if not candidates:
        return []
    listed = "\n".join(
        f'{i}. label="{c.get("label","")}" value="{c.get("value","")}" '
        f'quote="{str(c.get("quote",""))[:300]}"'
        for i, c in enumerate(candidates)
    )
    out = ollama.chat_json(
        model, _CRITIC_NUMBERS_SYS,
        f"Story: {story.get('headline')}\n\nArticle text:\n{bodies}\n\n"
        f"Candidate facts:\n{listed}",
        temperature=0.1, think=think,
    )
    verdicts = out.get("facts", []) if isinstance(out, dict) else []
    kept: list[dict] = []
    for i, c in enumerate(candidates):
        v = verdicts[i] if i < len(verdicts) and isinstance(verdicts[i], dict) else {}
        if bool(v.get("keep", False)):
            kept.append(c)
    return kept


def numeric_facts(ollama: Ollama, model_extract: str, model_critic: str, story: dict,
                  arts: list[Article], think_extract: str | None = None,
                  think_critic: str | None = None) -> list[dict]:
    """Full by-the-numbers pipeline for one story → verified facts
    [{label, value, quote}]. Extractor proposes, code grounds each quote in the
    source text, critic audits the survivors. Returns [] unless the story clears
    MIN_NUMERIC_FACTS, so a thin story just doesn't get the panel.

    Only the story's TWO strongest-matched articles are mined (arts is match-sorted).
    Facts are the pass most sensitive to a stray body — a loosely-matched third
    article would contribute figures that read as this story's own — so precision
    beats corroboration here."""
    bodies = _bodies_text(arts[:2])
    if not bodies.strip():
        return []

    with timed("extract"):
        candidates = extract_numbers(ollama, model_extract, story, bodies, think_extract)

    # Deterministic gate first: a candidate whose quote isn't really in the text is
    # a hallucination — drop it before spending the critic on it.
    body_norm = _norm_text(bodies)
    grounded = []
    for f in candidates:
        label, value = str(f.get("label", "")).strip(), str(f.get("value", "")).strip()
        if label and value and _grounded(str(f.get("quote", "")), body_norm):
            grounded.append({"label": label[:60], "value": value[:40],
                             "quote": str(f.get("quote", "")).strip()})

    with timed("critic"):
        verified = critic_numbers(ollama, model_critic, story, bodies, grounded, think_critic)
    return verified if len(verified) >= MIN_NUMERIC_FACTS else []


# ── pass 3e: headline continuity (historian + adversarial verifier) ──────────
# Two turns, both gpt-oss, mirroring extract→critic: the HISTORIAN judges the
# one thing code can't — is a keyword-matched past story really the SAME
# ongoing event, or coincidental topic overlap? — and drafts a one-sentence
# continuity note. Day counts and trend direction are never trusted from the
# model: news.history.ground_truth computes them from the stored record, and
# a mismatch between the historian's trend claim and that ground truth is
# rejected before the verifier is even spent. The VERIFIER then adversarially
# re-judges is_continuation, same skeptical-by-default posture as the numbers
# critic. Either check failing drops continuity for that story entirely — it
# just renders with no continuity note/panel, same fail-soft rule as every
# other enricher.

_HISTORIAN_SYS = (
    "You are a news editor deciding whether TODAY's story is a continuation of "
    "stories your desk already covered on past days, or just a coincidental topic "
    "overlap. You are given today's story and a list of CANDIDATE past stories "
    "that share keywords with it (each with its date, headline, and importance "
    "1-5). Decide is_continuation=true ONLY if the candidates are genuinely "
    "reporting on the SAME ongoing event or situation as today's story — not "
    "merely the same topic area (e.g. two different Fed stories a week apart "
    "about DIFFERENT policy actions are NOT a continuation; a team's game "
    "yesterday and a DIFFERENT game today are NOT a continuation). If true, "
    "judge whether the story's importance has been rising, falling, or holding "
    "steady across the days, and write ONE sentence framing the continuity for "
    "a reader (e.g. 'Fourth consecutive day of coverage, escalating from a "
    "routine update to today's lead story.'). If is_continuation=false, set "
    "trend='steady' and note=''. Return ONLY JSON "
    '{"is_continuation","trend","note"}.'
)


def historian_pass(ollama: Ollama, model: str, story: dict, candidates: list[dict],
                   think: str | None = None) -> dict:
    """Judges whether `candidates` (real stored past occurrences, already
    matched by news.history.find_candidates — never invented here) are
    genuinely the same ongoing story as `story`, and drafts a continuity
    note. Short-circuits with no ollama call when there are no candidates."""
    if not candidates:
        return {"is_continuation": False, "trend": "steady", "note": ""}
    listed = "\n".join(
        f'- {c["day"]}: "{c["headline"]}" (importance {c["importance"]}/5)'
        for c in candidates
    )
    user = (
        f"Today's story: {story.get('headline')}\n"
        f"Domain: {story.get('domain', '')}\n"
        f"Importance: {story.get('importance')}/5\n\n"
        f"Candidate past stories (share keywords with today's):\n{listed}"
    )
    out = ollama.chat_json(model, _HISTORIAN_SYS, user, temperature=0.2, think=think)
    trend = str(out.get("trend", "")).strip().lower()
    return {
        "is_continuation": bool(out.get("is_continuation")),
        "trend": trend if trend in ("rising", "falling", "steady") else "steady",
        "note": str(out.get("note") or "").strip()[:240],
    }


_VERIFY_CONTINUATION_SYS = (
    "You are a skeptical editor double-checking a colleague's claim that TODAY's "
    "story is a continuation of specific past stories. You are given today's "
    "story, the past stories claimed as the same ongoing event, and the "
    "colleague's one-sentence note. You are also given the REAL day-count and "
    "first-seen date, computed independently — reject if the note's claimed "
    "timeframe (e.g. 'third day', 'since Tuesday') contradicts these real "
    "numbers. Reject (confirmed=false) UNLESS the past stories are clearly "
    "about the SAME event or situation as today's, not just the same general "
    "topic — when genuinely uncertain, reject. Also reject if the note asserts "
    "anything not supported by the listed stories. Return ONLY JSON "
    '{"confirmed"}.'
)


def verify_continuation(ollama: Ollama, model: str, story: dict, candidates: list[dict],
                        historian_out: dict, ground: dict, think: str | None = None) -> bool:
    """Adversarial audit of the historian's is_continuation claim — same
    relevance-judge role critic_numbers plays for extracted figures. Also
    hands the model the REAL, code-computed day-count/first-seen (`ground`,
    from news.history.ground_truth) and instructs it to reject a note whose
    claimed timeframe contradicts them — a note claiming "Fourth consecutive
    day" when the real days_running is 2 is caught here, not just eyeballed.
    Never called (no ollama spend) when the historian already said no."""
    if not candidates or not historian_out.get("is_continuation"):
        return False
    listed = "\n".join(
        f'- {c["day"]}: "{c["headline"]}" (importance {c["importance"]}/5)'
        for c in candidates
    )
    user = (
        f"Today's story: {story.get('headline')}\n\n"
        f"Claimed past occurrences:\n{listed}\n\n"
        f"Colleague's note: \"{historian_out.get('note', '')}\"\n\n"
        f"REAL facts, computed independently in code — the note must not "
        f"contradict these:\n"
        f"- days_running: {ground.get('days_running')}\n"
        f"- first_seen: {ground.get('first_seen')}"
    )
    out = ollama.chat_json(model, _VERIFY_CONTINUATION_SYS, user,
                           temperature=0.1, think=think)
    return bool(out.get("confirmed"))


def _dedup_candidates_by_day(candidates: list) -> list:
    """One candidate per distinct day — a day's triage occasionally splits one
    real event into two stored slugs, which would otherwise make days_running
    (distinct days) disagree with len(candidates) (raw rows). Keeps the
    highest-importance candidate per day; ties keep whichever is encountered
    first (order doesn't matter — importance drives the choice)."""
    by_day: dict[str, object] = {}
    for c in candidates:
        existing = by_day.get(c.entry.day)
        if existing is None or c.entry.importance > existing.entry.importance:
            by_day[c.entry.day] = c
    return list(by_day.values())


def continuity_facts(ollama: Ollama, model_historian: str, model_verifier: str,
                     story: dict, entities: list[str], window: list,
                     match_threshold: float, think_historian: str | None = None,
                     think_verifier: str | None = None) -> dict | None:
    """Full continuity pipeline for one story → a `_continuity` dict, or
    None. Code matches candidates (news.history.find_candidates) and computes
    the ground-truth day-count/trend; the historian judges is_continuation +
    drafts the note; a code-side trend check runs before spending the
    verifier call; the verifier then adversarially audits is_continuation.
    Any failure at any step → None, so the story just renders with no
    continuity data — same fail-soft posture every enricher already has."""
    candidates = _history.find_candidates(
        story.get("headline", ""), story.get("subject", ""), entities,
        window, match_threshold)
    if not candidates:
        return None

    # A day's triage occasionally splits one real event into two stored slugs.
    # Dedupe to one candidate per distinct day (highest importance wins) BEFORE
    # anything downstream reads day-counts, so ground["days_running"],
    # len(candidate_dicts), the offer text, and the rendered timeline can never
    # disagree — len(occurrences) + 1 == days_running by construction.
    candidates = _dedup_candidates_by_day(candidates)

    candidate_dicts = [{"day": c.entry.day, "headline": c.entry.headline,
                        "importance": c.entry.importance} for c in candidates]

    with timed("historian"):
        h_out = historian_pass(ollama, model_historian, story, candidate_dicts,
                               think_historian)
    if not h_out["is_continuation"]:
        return None

    ground = _history.ground_truth(int(story.get("importance") or 0), candidates)
    if not _history.trend_matches(h_out["trend"], ground):
        return None            # historian's own trend claim already inconsistent

    with timed("verifier"):
        confirmed = verify_continuation(ollama, model_verifier, story,
                                        candidate_dicts, h_out, ground, think_verifier)
    if not confirmed:
        return None

    return {
        "is_continuation": True,
        "days_running": ground["days_running"],
        "first_seen": ground["first_seen"],
        "trend": h_out["trend"],
        "note": h_out["note"],
        "occurrences": candidate_dicts,
    }


# ── pass 3b: sentiment ──────────────────────────────────────────────────────────
# A separate editorial pass (qwen3:8b, not gemma) run once over the whole day. It
# tags each story good/bad/neutral so run.py can tint that story's metric/chart
# panels — the "good news is green, bad news is red" read. Batched into one call
# (not per-story) so the bigger model is resident only briefly.

_SENTIMENT_SYS = (
    "You are a news editor tagging the TONE of each story for a reader's "
    "dashboard. For each story decide whether the news is, on balance, good, "
    "bad, or neutral FROM A GENERAL READER'S PERSPECTIVE — a peace deal, a "
    "cure, a team winning, a strong earnings beat = good; a disaster, a "
    "scandal, a crash, a loss, a layoff = bad; procedural, mixed, or ambiguous "
    "= neutral. Judge the events, not the writing. Return ONLY JSON "
    '{"sentiments":[{"slug","sentiment"}]} with a sentiment for EVERY slug.'
)


def sentiment_pass(ollama: Ollama, model: str, stories: list,
                   think: str | None = None) -> dict[str, str]:
    """Tag every story good/bad/neutral in one call. Returns {slug: sentiment};
    slugs the model omits or mislabels default to neutral in norm_sentiment."""
    from .plan_schema import norm_sentiment

    if not stories:
        return {}
    lines = [
        f'- slug="{s.slug}" [{s.domain}] {s.headline}'
        + (f" — {s.summary[:160]}" if s.summary else "")
        for s in stories
    ]
    out = ollama.chat_json(
        model, _SENTIMENT_SYS,
        "Stories:\n" + "\n".join(lines),
        temperature=0.1, think=think,
    )
    tags: dict[str, str] = {}
    for item in (out.get("sentiments", []) if isinstance(out, dict) else []):
        if isinstance(item, dict) and item.get("slug"):
            tags[str(item["slug"])] = norm_sentiment(item.get("sentiment"))
    return tags


# ── pass 3c: curator (editor-in-chief) ──────────────────────────────────────────
# The pass that makes the split-dashboard day feel edited rather than dumped. Also
# qwen3:8b. Given the day's stories (already routed to section boards in code), it
# (1) picks the front-page "Top Stories" digest across all sections and (2) writes
# a one-line editor's brief per board. Routing stays in code (deterministic from
# domain); the model does the judgement calls a code rule can't — "which of these
# is actually the day's lead" and "how would an editor frame this section today".

_CURATOR_SYS = (
    "You are the editor-in-chief laying out today's news dashboards. You are "
    "given the day's stories, each already assigned to a section board. Do two "
    "things:\n"
    "1. Pick the {n} strongest stories overall for the front-page digest — the "
    "ones a reader must see first. Spread them across sections; don't take all "
    "from one. A story marked 'running Nth day' that ISN'T escalating (trend "
    "rising) is often a rehash — weigh a fresh lead story over a stale one at "
    "similar importance, though a big story can legitimately dominate for days.\n"
    "2. Write a ONE-sentence editor's brief for EACH board named below, framing "
    "what its stories add up to today (e.g. 'Washington is consumed by the "
    "shutdown fight while the Middle East ceasefire holds'). If a board has no "
    "stories, write a short 'quiet day' line for it.\n"
    'Return ONLY JSON {"overview":[slug,...],"briefs":{"<board>":"<sentence>"}} '
    "— use the board names EXACTLY as given."
)


def curate(ollama: Ollama, model: str, stories_brief: list[dict],
           boards: list[str], overview_size: int, think: str | None = None) -> dict:
    """Editor-in-chief pass. `stories_brief` is [{slug,board,headline,subject,
    importance,breaking,summary,days_running,trend}] — days_running/trend are
    the code-computed continuity fatigue signal (news.run._continuity_brief_fields,
    itself sourced from news.history.ground_truth) the prompt reads to weigh a
    fresh lead against a stale, non-escalating rehash. Returns
    {"overview":[slug,...], "briefs":{board: sentence}} — callers must
    tolerate missing keys."""
    if not stories_brief:
        return {"overview": [], "briefs": {}}
    lines = []
    for s in stories_brief:
        flag = " BREAKING" if s.get("breaking") else ""
        running = f" (running day {s['days_running']}, {s.get('trend', '')})" \
            if s.get("days_running", 0) >= 2 else ""
        lines.append(
            f'- slug="{s["slug"]}" board="{s["board"]}" '
            f'importance={s.get("importance", 3)}/5{flag}{running}: {s.get("headline", "")}'
            + (f" — {s['summary'][:160]}" if s.get("summary") else "")
        )
    user = (
        "Boards: " + ", ".join(f'"{b}"' for b in boards) + "\n\n"
        "Stories:\n" + "\n".join(lines) + "\n\n"
        "Assemble the front page and write each board's brief."
    )
    out = ollama.chat_json(
        model, _CURATOR_SYS.replace("{n}", str(overview_size)), user,
        temperature=0.3, think=think,
    )
    if not isinstance(out, dict):
        return {"overview": [], "briefs": {}}
    overview = [s for s in out.get("overview", []) if isinstance(s, str)]
    briefs = {k: str(v).strip() for k, v in (out.get("briefs") or {}).items()
              if isinstance(k, str) and str(v).strip()}
    return {"overview": overview, "briefs": briefs}


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
    "- collection: a grid of stat tiles (one per figure). Wants w=4-6; height "
    "grows with the number of tiles — around h=5-8.\n"
    "- image: a photo. Wants w=4-6 and h=7-10; too short and it crops badly.\n"
    "- table: ~2 rows of header plus 1 per data row.\n"
    "- Give the lead story (importance 5) more room than a minor one.\n"
    "- Pair a story's markdown with its visual on the SAME line (e.g. 6 + 6) so "
    "they read together, rather than stacking every panel full width.\n\n"
    'Return ONLY JSON: {"panels":[{"id":<int>,"w":<int>,"h":<int>}]} — one entry '
    "for every id given, no extras."
)


def layout(ollama: Ollama, model: str, manifest: list[dict],
           think: str | None = None) -> dict[int, tuple[int, int]]:
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
        temperature=0.3, think=think,
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


# A story about a quantifiable phenomenon (inflation, gas, oil) gets a real data
# series only when the phenomenon is CENTRAL — a configured keyword in the
# headline or an article TITLE — so an incidental mention doesn't drag in a chart.
# Sports is excluded outright: "gold"/"record" etc. collide with medal/stat
# language, and a series has no place on a game story anyway.
SERIES_SKIP_DOMAINS = {"sports"}


def _central_series(headline: str, arts: list[Article], config: dict) -> list[dict]:
    """Configured data series whose keyword is central to this story → the series
    specs to offer. Same centrality test as `_central_tickers` (headline or an
    article title, not the loose ranking match)."""
    hay = (headline + " " + " ".join(a.title for a in arts)).lower()
    out = []
    for spec in config.get("series", []):
        if not spec.get("provider") or not spec.get("id"):
            continue
        if any(str(kw).lower() in hay for kw in spec.get("keywords", [])):
            out.append(spec)
    return out


def _wants_research(story: dict, has_series: bool, config: dict, spent: int) -> bool:
    """Whether to spend a research call on this story. Gated hard: only when the
    agent is enabled, no configured series already matched, the story is a
    lead/breaking item, the domain isn't excluded, and the per-run budget holds —
    each call is a real, billable Claude web-search request."""
    rc = config.get("research", {})
    if not rc.get("enabled") or has_series:
        return False
    if str(story.get("domain", "")).lower() in SERIES_SKIP_DOMAINS:
        return False
    if spent >= int(rc.get("max_per_run", 2)):
        return False
    return (bool(story.get("breaking"))
            or int(story.get("importance") or 0) >= int(rc.get("min_importance", 4)))


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
    """Run the per-story passes (triage → facts extract/critic → planner →
    summarizer) and return a validated DayPlan."""
    defaults = config.get("defaults", {})
    oc = config.get("ollama", {})
    models = config.get("models", {})
    effort = config.get("reasoning", {})
    ollama = Ollama(oc.get("host", "http://localhost:11434"),
                    oc.get("timeout_seconds", 180), oc.get("num_ctx"))

    candidates = rank_articles(articles, defaults.get("triage_candidates", 80))
    # Triage is told which section boards exist and reserves each a slot budget —
    # without that it ranks globally and a lopsided news day leaves whole boards
    # empty. Routing is derived from the same config run.py routes on, so the
    # two can never disagree about which domains reach which board.
    with timed("triage"):
        stories = triage(ollama, models.get("triage", "gpt-oss:latest"),
                         candidates, defaults.get("top_stories", 8), effort.get("triage"),
                         routing=domain_to_board(config),
                         per_section=int(defaults.get("per_section_stories", 2)))

    # Re-derive which articles belong to each story from content, not from triage's
    # fragile index list (see assign_articles). A story that matches no article is
    # dropped — better a missing story than one confabulated from a mismatched body.
    membership = assign_articles(stories, candidates)

    plan = DayPlan(day=run_day or date.today())
    research_spent = 0                        # per-run budget for the research agent
    hist_cfg = config.get("history", {})
    history_window = _history.load_window(plan.day, int(hist_cfg.get("retention_days", 60)))
    for si, st in enumerate(stories):
        arts = membership.get(si, [])[:12]
        if not arts:
            continue

        # Pull real article bodies for this story's lead articles — the summarizer
        # and the extractor reason off substance instead of RSS teasers.
        with timed("hydrate"):
            hydrate_bodies(arts, defaults.get("body_articles", 3))

        # By-the-numbers: extract the story's key figures and fact-check them
        # before they can be offered. Grounded + critic-audited, so the panel only
        # ever shows numbers that trace to a source sentence.
        facts = numeric_facts(
            ollama, models.get("extractor", "gpt-oss:latest"),
            models.get("critic", "gpt-oss:latest"), st, arts,
            effort.get("extractor"), effort.get("critic"),
        )

        tickers = (_central_tickers(st.get("headline", ""), arts, config)
                   if _wants_stock(st, config) else {})
        series_specs = ([] if str(st.get("domain", "")).lower() in SERIES_SKIP_DOMAINS
                        else _central_series(st.get("headline", ""), arts, config))

        entities = _history.entities_from_articles(arts)
        # `st` is the raw triage dict — it has no "subject" key yet (the
        # summarizer, which fills that in, hasn't run at this point in the
        # sequence), so story.get("subject", "") below is always "" and
        # matching runs on headline+entities only, not headline+subject+
        # entities like the stored side. Intentional given the pass ordering
        # established by Task 7 (changing it risks group_entries' symmetry
        # for the weekly recap); the practical exposure is bounded by
        # history.MAX_CANDIDATES.
        continuity = continuity_facts(
            ollama, models.get("historian", "gpt-oss:latest"),
            models.get("verifier", "gpt-oss:latest"), st, entities, history_window,
            float(hist_cfg.get("match_threshold", 0.35)),
            effort.get("historian"), effort.get("verifier"),
        )

        # Long-tail fallback: when no configured series matched, let the research
        # agent (Claude + web search) look for a real, source-verified series.
        # Off by default and budget-gated; returns None unless it clears the
        # authoritative-domain + quote-grounding honesty gate (providers.research).
        research_series = None
        if _wants_research(st, bool(series_specs), config, research_spent):
            from .providers import research as _research_provider
            with timed("research"):
                research_series = _research_provider.research_series(
                    st, _bodies_text(arts[:2]), config)
            if research_series is not None:
                research_spent += 1

        has_image = any(getattr(a, "image_url", "") for a in arts)
        offers = story_offers(st, arts, tickers, has_image, len(facts), series_specs,
                              research_label=(research_series.label if research_series else ""),
                              history_occurrences=len(continuity["occurrences"]) if continuity else 0)

        with timed("planner"):
            panels = plan_story(ollama, models.get("planner", "gpt-oss:latest"), st, arts,
                                offers, effort.get("planner"))
        with timed("summarizer"):
            summ = summarize_story(ollama, models.get("summarizer", "gpt-oss:latest"), st,
                                   arts, effort.get("summarizer"))

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
            # keep the clustered articles + verified facts on the spec — the
            # coverage/facts enrichers and hero_image() read them straight off it.
            spec._articles = arts     # type: ignore[attr-defined]
            spec._facts = facts       # type: ignore[attr-defined]
            spec._research = research_series  # type: ignore[attr-defined]
            spec._continuity = continuity      # type: ignore[attr-defined]
            plan.stories.append(spec)
    return plan
