# Reddit tracker — design

Status: **design / spec** (not yet built). Adds a lightweight, independent
pipeline that tracks a fixed list of subreddits (starting with r/archlinux,
r/marvelrivals, r/thefinals) for official patch notes / major updates, and
renders them on a new "Community" board.

## Thesis

The existing news pipeline is built around clustering *multiple outlets*
covering the *same event* — that's what triage/extract/planner exist for.
Reddit subs don't fit that shape: each sub is a single source, and the vast
majority of daily posts (memes, discussion threads, routine chatter) are not
announcement-worthy. Running these through triage→extract→critic→planner
would be the wrong tool — expensive, and solving a clustering problem that
doesn't exist here. Instead this is a **fetch → filter → digest** pipeline,
independent of and running alongside the main news `fetch_all`.

## Fetch

New `news/reddit.py`, structurally parallel to `fetch.py`. For each configured
sub, pulls `https://www.reddit.com/r/{subreddit}/hot/.rss?limit={candidates_per_sub}`.

**Gotcha to build in from the start:** reddit's RSS endpoint 429s the default
`urllib`/`feedparser` user agent. Fetch with `requests` using a real
`User-Agent` header, then hand the response bytes to
`feedparser.parse(resp.content)` — same two-step pattern, just not
`feedparser.parse(url)` directly.

Normalizes each entry to a small `RedditPost`: `id, title, url, subreddit,
published, snippet` (best-effort short selftext extracted from the entry
body, empty for link posts).

## Filter — batched classification (gemma, low reasoning effort)

One call per sub (3 total per run), input = that sub's hot candidates
(title + snippet), output = which are genuine announcements (patch notes,
major updates, official advisories) vs. discussion/memes/routine posts, each
with a one-line reason:

```json
{
  "confirmed": [
    {"id": "abc123", "reason": "Official patch notes thread for the 2.3 update"}
  ]
}
```

This is a batch judgement over an already-small candidate set (`hot` sort
already filters out the long tail), same shape as `triage`'s per-day
clustering call but far narrower — a model is needed here because titles
don't reliably say "patch notes" (Arch security advisories, "Season 3.5"
naming) and a fixed keyword list would miss/false-positive too often.

Reasoning effort: **low** — this is closer to `layout`'s mechanical
classification than to `triage`'s clustering judgement.

## Dedup

`state/reddit_seen.json`: `{subreddit: {post_id: first_seen_date}}`. A post
confirmed once is rendered once; subsequent runs skip it while it's still
`hot`. Entries older than `reddit.seen_retention_days` (default 14) are
pruned on write — same cache-with-bounded-retention pattern as
`state/bodies/`.

## Render

New "Community" board (alongside Overview/Politics/Tech/Sports/Markets). One
compact markdown panel per subreddit, **skipped entirely if that sub had zero
confirmed posts today** — same fail-soft rule every enricher already follows.
Each panel lists confirmed posts newest-first: title (linked) + the
classifier's one-line reason. No fact extraction, no data panels, no
per-story layout-pass sizing beyond the existing markdown fallback — this is
a digest, not a story.

## Pipeline placement

Runs independently alongside `fetch_all` in `run.build_plan` — it doesn't
touch `plan.stories` or any of the gemma story passes. Board build happens in
`apply_plan` alongside the other boards, using the confirmed-posts list
directly (no `StorySpec`/`PanelSpec` involved).

## Config

New `reddit:` block in `outlets.yaml`:

```yaml
reddit:
  enabled: true
  candidates_per_sub: 15
  seen_retention_days: 14
  subs:
    - { name: "Arch Linux",    subreddit: "archlinux" }
    - { name: "Marvel Rivals", subreddit: "marvelrivals" }
    - { name: "The Finals",    subreddit: "thefinals" }
```

## Cost

Per run: 3 RSS fetches (cheap, no auth) + 3 small batched gemma calls (one
per sub, over an already-small hot-sorted candidate set). Independent of the
main news pipeline's ~20-minute budget — this adds well under a minute.

## Open questions

None outstanding — design approved section-by-section during brainstorming.
