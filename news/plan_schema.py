"""The planner contract — the single source of truth for what the gemma
"planner" pass is allowed to emit, and the shape `run.py` builds from.

The whole "alive" behaviour hinges on this: the planner decides, per story,
which panels a dashboard needs and what data each binds to. Every panel's
``data`` key is ``"<enricher>:<arg>"``; the enricher registry (news.enrichers)
resolves it to helio columns+rows. Anything the planner emits that doesn't
validate here is dropped, so a hallucinated plan degrades to headlines-only
rather than breaking the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Panel types helio supports (from the MCP `create_panel` enum). `image` is an
# unbound content panel (config.imageUrl/imageFit) — no data source needed.
DATA_PANEL_TYPES = {"metric", "chart", "table"}
TEXT_PANEL_TYPES = {"text", "markdown"}
ALLOWED_PANEL_TYPES = DATA_PANEL_TYPES | TEXT_PANEL_TYPES | {"image", "divider"}

# Chart shapes helio accepts (backend RequestValidation.ValidChartTypeValues).
# NB: setting one requires PATCHing a COMPLETE ChartAppearance — a partial
# {"chartType": …} is rejected with a 400. See helio_client.set_chart_type.
CHART_TYPES = {"line", "bar", "pie", "scatter"}

# Enricher prefixes the planner may reference in a panel's ``data`` key. Keep in
# sync with news.enrichers.REGISTRY. Unknown prefixes are dropped in validation.
# Headlines and summaries are rendered into each story's markdown panel by
# run.py (not bound data panels), so they are not enrichers.
KNOWN_ENRICHERS = {"stock", "coverage", "facts", "series", "research"}

DOMAINS = {"politics", "sports", "tech", "ai", "markets", "business", "world", "general"}

# Story-level sentiment, set by the `sentiment` gemma/qwen pass (news.agents).
# Drives the background tint of a story's metric/chart panels only — `neutral`
# leaves the panel on the default surface. A stock panel overrides this with its
# own price-movement sign (up = good, down = bad); see run.py.
SENTIMENTS = {"good", "bad", "neutral"}


def norm_sentiment(v) -> str:
    s = str(v or "").strip().lower()
    return s if s in SENTIMENTS else "neutral"


@dataclass
class PanelSpec:
    """One panel on a story's dashboard section."""

    type: str
    title: str
    data: str | None = None          # "<enricher>:<arg>", None for text/image/divider
    content: str | None = None       # inline body for text/markdown panels
    chart_type: str | None = None    # line | bar | pie | scatter (chart panels)
    image_url: str | None = None     # image panels only
    # Grid hints. The layout pass sets these; run.py packs them into the grid.
    w: int | None = None
    h: int | None = None

    def enricher(self) -> str | None:
        if not self.data or ":" not in self.data:
            return None
        return self.data.split(":", 1)[0]

    @classmethod
    def from_dict(cls, raw: dict) -> "PanelSpec | None":
        """Tolerant parse of one planner-emitted panel; returns None if it can't
        be salvaged into something bindable/renderable."""
        ptype = str(raw.get("type", "")).strip().lower()
        if ptype not in ALLOWED_PANEL_TYPES:
            return None
        title = str(raw.get("title") or "").strip() or ptype.title()
        data = raw.get("data")
        content = raw.get("content")

        if ptype in DATA_PANEL_TYPES:
            if not isinstance(data, str) or ":" not in data:
                return None
            if data.split(":", 1)[0] not in KNOWN_ENRICHERS:
                return None
        if ptype in TEXT_PANEL_TYPES and not content:
            return None

        chart_type = str(raw.get("chart_type") or raw.get("chartType") or "").strip().lower()

        return cls(
            type=ptype,
            title=title[:80],
            data=data if isinstance(data, str) else None,
            content=str(content) if content else None,
            chart_type=chart_type if chart_type in CHART_TYPES else None,
            w=_as_int(raw.get("w")),
            h=_as_int(raw.get("h")),
        )


@dataclass
class StorySpec:
    """A clustered story plus the panels chosen to render it."""

    slug: str
    headline: str
    domain: str
    subject: str = ""                 # short theme, used as the panel/section title
    importance: int = 3               # 1 (minor) .. 5 (lead)
    breaking: bool = False            # new/developing today (vs. ongoing coverage)
    sentiment: str = "neutral"        # good | bad | neutral — tints data panels
    summary: str = ""
    article_urls: list[str] = field(default_factory=list)
    panels: list[PanelSpec] = field(default_factory=list)

    def title(self) -> str:
        """Section title: the theme/subject, falling back to the headline."""
        return (self.subject or self.headline)[:80]

    def hero_image(self) -> str:
        """Lead image for the story: the first clustered article that carried one.
        Articles arrive importance-ordered within a story, and several of our
        feeds (ESPN, CNBC, TechCrunch) carry no images at all — so this scans the
        whole cluster rather than trusting the top article."""
        for a in getattr(self, "_articles", None) or []:
            if getattr(a, "image_url", ""):
                return a.image_url
        return ""

    @classmethod
    def from_dict(cls, raw: dict) -> "StorySpec | None":
        slug = _slugify(raw.get("slug") or raw.get("story") or raw.get("headline") or "")
        if not slug:
            return None
        domain = str(raw.get("domain", "general")).strip().lower()
        if domain not in DOMAINS:
            domain = "general"
        # Only data panels survive here now (stock); headlines/summary become the
        # story's markdown panel, built in run.py.
        panels = [p for p in (PanelSpec.from_dict(x) for x in raw.get("panels", [])
                              if isinstance(x, dict)) if p]
        return cls(
            slug=slug,
            headline=str(raw.get("headline") or raw.get("story") or slug).strip()[:200],
            domain=domain,
            subject=str(raw.get("subject") or "").strip()[:80],
            importance=max(1, min(5, _as_int(raw.get("importance")) or 3)),
            breaking=bool(raw.get("breaking")),
            sentiment=norm_sentiment(raw.get("sentiment")),
            summary=str(raw.get("summary") or "").strip(),
            article_urls=[u for u in raw.get("article_urls", []) if isinstance(u, str)],
            panels=panels,
        )


@dataclass
class DayPlan:
    """The full plan for one day's dashboard."""

    day: date
    stories: list[StorySpec] = field(default_factory=list)

    def resource_prefix(self) -> str:
        """Name stamp for every helio resource this run creates. Constant (not
        dated): each run wipes all `news-*` resources then rebuilds, so cleanup
        matches by this stable prefix rather than by date."""
        return "news"


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in str(text).strip()]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60]
