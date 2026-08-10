# News v4 — adopt helio-mcp v1.6 primitives (design)

## Context

The news pipeline (`news/helio_client.py`, `news/run.py`) was written against
helio-mcp `release/v1.5` semantics (see `docs/news-v3-plan.md`). The live MCP
server the pipeline actually connects to (`helio-mcp/dist/index.js`, built
from `helio` `main`) is now three no-op commits ahead of `release/v1.6` — so
the running server already exposes the full v1.6 tool surface, but the
client code never adopted it. This is an audit-driven cleanup: no new
product behavior, no new panels — just replacing hand-rolled multi-call
chains with the single-call primitives v1.6 added for exactly this purpose.

## Scope

**In scope** (all four independently shippable, no ordering dependency):

1. `create_bound_panel` replaces `HelioClient.build_bound_panel()`'s manual
   6-7 call chain.
2. `auto_layout_dashboard` replaces the local `_pack()`/`_fill_shelf()`
   shelf-packing algorithm in `run.py`.
3. `create_panels` (batch) replaces the two sequential `create_panel` calls
   for a story's image+markdown pair in `_build_story()`.
4. A new standalone dev-time script wraps `get_panel_capabilities` for
   auditing an enricher's `mapping`/`columns` when developing a new panel
   type. Not called from the pipeline.

**Explicitly out of scope**, decided during design and recorded here so
nobody re-proposes them without re-reading this reasoning:

- **Tag-based `teardown_resources`.** `create_bound_panel`'s schema has no
  `tag` parameter (it shipped in PR #300; resource tagging, HEL-366, shipped
  two PRs later in #302). Tagging the DataSource only and passing
  `sourceDataSourceId` into `create_bound_panel` doesn't work either —
  `teardown_resources` refuses its **entire** call (deletes nothing) if a
  tagged resource has an untagged dependent outside the tag batch, and the
  pipeline/DataType `create_bound_panel` creates internally would always be
  untagged. Getting tag-based teardown would require reverting to the
  granular chain, which is the exact simplification item 1 removes. Keep
  `cleanup_news_resources()` (name-pattern sweep) as-is — it already works,
  is idempotent, and deliberately over-matches ad-hoc/probe resources.
- **`create_pipeline_from_shape("time-series", ...)`.** Same incompatibility
  as above (the shape only produces a standalone pipeline via the granular
  chain). Zero behavioral gain anyway: `series.py`'s hand-written
  `groupBy month + avg` + `sort` steps are already the same steps the shape
  would expand into, and they already flow straight into
  `create_bound_panel`'s `pipeline.steps` unchanged.
- **`replace_dashboard_contents`.** Its panel shape is a static declarative
  list (`dataTypeId` + `fieldMapping`, or `content`, or `url`) — it doesn't
  run pipelines. Doesn't fit an architecture where panels are pipeline-bound
  against progressively-fetched real data.
- **`get_panel_capabilities` wired into the pipeline.** `create_bound_panel`
  already validates the field mapping atomically before creating anything
  and cleans up fully on failure — the same guarantee a runtime
  pre-flight check would add, at the cost of an extra round-trip per panel
  and (again) needing the granular chain to get an intermediate DataType id
  to introspect.

## Component changes

### `news/helio_client.py`

**`build_bound_panel`** — rewritten to one call:

```python
async def build_bound_panel(self, dashboard_id: str, prefix: str, title: str,
                            sd, background: str = "") -> str:
    appearance: dict = {}
    if sd.panel_type == "chart":
        appearance["chart"] = {**CHART_APPEARANCE, "chartType": sd.chart_type}
    if background:
        appearance["background"] = background
    panel_args: dict = {"type": sd.panel_type, "title": title}
    config = sd.panel_config()
    if config:
        panel_args["config"] = config
    if appearance:
        panel_args["appearance"] = appearance
    result = await self.call("create_bound_panel", {
        "dashboardId": dashboard_id,
        "source": {"name": f"{prefix}-src-{sd.key}", "columns": sd.columns, "rows": sd.rows},
        "pipeline": {"name": f"{prefix}-pipe-{sd.key}",
                     "outputDataTypeName": _type_name(prefix, sd.key),
                     "steps": sd.pipeline_steps()},
        "panel": panel_args,
        "fieldMapping": sd.mapping,
    })
    return result["panel"]["id"]
```

`_apply_appearance()` is deleted — its logic (complete `ChartAppearance`
required on every PATCH, chart type + background must travel together) is
now inlined into the single `appearance` dict built above, still using the
existing `CHART_APPEARANCE` default-merge constant. The exact response
shape (`result["panel"]["id"]`) gets confirmed against a live probe before
the implementation task is marked done — `create_bound_panel`'s tool
description says "returns every created panel" for the batch variant but is
less explicit for the single-panel bound-panel response; Task 1 in the plan
includes a throwaway-probe verification step.

**`set_layout`** — same signature, new backing call:

```python
async def set_layout(self, dashboard_id: str, items: list[dict]) -> None:
    """items: [{panelId, w, h}] — sizes only, no x/y. The server computes
    placement (auto_layout_dashboard, v1.6)."""
    if "auto_layout_dashboard" not in await self.tool_names():
        return
    await self.call("auto_layout_dashboard",
                    {"dashboardId": dashboard_id, "items": items})
```

The `items` contract changes from `{panelId, x, y, w, h}` to
`{panelId, w, h}` — callers stop computing `x`/`y`. The tool-availability
guard pattern (`if "..." not in await self.tool_names(): return`) is kept
unchanged from the current `update_dashboard_layout` guard, just retargeted.

**New: batched story panels.** `add_image_panel`/`add_text_panel` stay (still
used standalone elsewhere — e.g. digest-only stories only add a markdown
panel, no image), but `_build_story()` in `run.py` gains a path that builds
both in one `create_panels` call when both are needed (see below). This
means `HelioClient` gains one new thin helper:

```python
async def add_story_panels(self, dashboard_id: str, *, image: dict | None,
                           markdown: dict) -> dict:
    """image: {"title", "url", "caption"} or None. markdown: {"title", "content"}.
    Returns {"image_id": str | None, "markdown_id": str}."""
    panels = []
    if image:
        cfg = {"imageUrl": image["url"], "imageFit": "cover"}
        if image.get("caption"):
            cfg["caption"] = image["caption"]
        panels.append({"type": "image", "title": image["title"], "config": cfg})
    panels.append({"type": "markdown", "title": markdown["title"],
                   "config": {"content": markdown["content"]}})
    result = await self.call("create_panels",
                             {"dashboardId": dashboard_id, "panels": panels})
    created = result["panels"] if isinstance(result, dict) else result
    if image:
        return {"image_id": created[0]["id"], "markdown_id": created[1]["id"]}
    return {"image_id": None, "markdown_id": created[0]["id"]}
```

Exact response-envelope shape (`result["panels"]` vs a bare list) gets
confirmed by a throwaway probe in the same implementation task, mirroring
how P1/P2's live-probe verification worked.

### `news/run.py`

**`_build_story()`** — replaces its two `helio.add_image_panel` /
`helio.add_text_panel` calls with one `helio.add_story_panels(...)` call,
passing the same caption/content logic that exists today (hero image
before summary, credit-strip caption, `story_markdown(story)`). Behavior
(what gets built, in what order, digest-mode skipping data panels) is
unchanged — only the call shape changes.

**`_pack()` and `_fill_shelf()` are deleted.** `_finish_board()` changes
from:

```python
await helio.set_layout(dashboard_id, _pack(built, sizes))
```

to a small pure helper that only clamps and sizes (no placement):

```python
def _sized_items(built: list[dict], sizes: dict[int, tuple[int, int]]) -> list[dict]:
    """Per-panel (w, h) after applying news's own product-tuned floors/
    ceilings (_BOUNDS/_FALLBACK) — no x/y; the server places them
    (auto_layout_dashboard)."""
    items = []
    for i, p in enumerate(built):
        w, h = sizes.get(i) or _FALLBACK.get(p["kind"], (6, 8))
        w, h = _clamp(p["kind"], w, h)
        items.append({"panelId": p["id"], "w": w, "h": h})
    return items
```

```python
await helio.set_layout(dashboard_id, _sized_items(built, sizes))
```

`GRID_COLS`, `_FALLBACK`, `_BOUNDS`, `_clamp()` are all kept unchanged —
these encode news-specific product judgment (e.g. "charts need h≥6 or they
clip axis labels") that the server's generic per-kind clamp doesn't know
about. `_FILL_THRESHOLD` is deleted along with `_fill_shelf()` — ragged-edge
widening is now the server's job.

### New: `scripts/audit_panel_capabilities.py` (dev-time only)

A small standalone script — not imported by `news/`, not run by the
scheduled pipeline. Takes a `--data-type-id` (or discovers one by name via
`list_data_types`), calls `get_panel_capabilities`, and pretty-prints the
bindable panel kinds + required/optional fieldMapping slots + eligible
columns. Exists so that when a new enricher is added, its `mapping` can be
checked against the real backend rules instead of trusting memorized
gotchas (e.g. the `chartOptions`-keyed-by-chart-type gotcha already in
project memory). Uses the same `HelioClient.session()` context manager as
the pipeline; reads `.env` the same way.

## Testing

- **`_sized_items()`** (pure, `run.py`): unit tests covering the fallback
  path (missing size → `_FALLBACK`), the clamp path (oversized/undersized
  input gets floored/ceilinged per `_BOUNDS`), and that no `x`/`y` keys
  appear in the output.
- **`HelioClient.build_bound_panel`**: test with a stub `session` whose
  `call_tool` is a spy — assert exactly one `create_bound_panel` call is
  made, with the expected `source`/`pipeline`/`panel`/`fieldMapping` shape
  built from a fixture `SourceData`, for both a chart panel (appearance
  present) and a table panel (no appearance).
- **`HelioClient.set_layout`**: stub `tool_names()` both ways — asserts
  `auto_layout_dashboard` is called with sizes-only items when the tool is
  present, and that nothing is called when absent (mirrors the existing
  `update_dashboard_layout` guard test pattern, if one exists — if not,
  this plan adds the first one for `set_layout` too).
- **`HelioClient.add_story_panels`**: stub spy — asserts one `create_panels`
  call with two entries when `image` is given, one entry when it's `None`,
  and that returned ids map back to the right key (`image_id`/`markdown_id`)
  under both the `{"panels": [...]}` and bare-list response shapes (guard
  against a live-probe surprise either way).
- **Live smoke test** (manual, same pattern as v3's P1/P2/P3 verification):
  after implementation, run `--plan-only` is not sufficient here since these
  are all helio-write paths — run a real (or throwaway-probe) build against
  a scratch dashboard, confirm panels render with correct binding/appearance
  and layout has no overlaps, then tear the scratch dashboard down.

## Error handling

`create_bound_panel`'s documented failure mode (validates before creating
anything; on failure after that gate, cleans up everything it created) means
`build_bound_panel` needs no new try/except — a failure raises the same way
`self.call()` already raises today (`RuntimeError` on `res.isError`), and
the caller (`_build_story`) has no per-panel error handling today either, so
none is added. `create_panels`' documented all-or-nothing batch failure
means `add_story_panels` behaves the same way — one bad panel in the pair
means neither is created, surfaced as the same `RuntimeError`.

## Non-goals

- No change to what panels are built, what data they show, or their visual
  config — this is a call-plumbing simplification only.
- No change to the gemma `layout` pass — it still sizes panels; it never
  placed them, so nothing about its prompt or output contract changes.
- No change to `cleanup_news_resources()`.
