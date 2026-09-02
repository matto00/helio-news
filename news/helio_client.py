"""Phase 3 — helio access, exclusively through the helio MCP server.

Python is the MCP *client*: it spawns `node dist/index.js` over stdio and calls
its tools. All auth stays inside that server process — we only hand it its env
(`HELIO_PAT`, `HELIO_API_BASE_URL`), never call helio's REST API directly.

Requires the delete tools added to the MCP server (create-fresh / delete-old
cleanup). The PAT is read from the environment or a gitignored `.env`; it never
lives in code or in config/outlets.yaml.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Mirrors the backend's ChartAppearance.Default. Required in full on every
# appearance PATCH (see set_chart_type) — the backend has no partial decoder for
# it, so sending just {"chartType": …} 400s.
CHART_APPEARANCE = {
    "seriesColors": ["#5470c6", "#91cc75", "#fac858", "#ee6666",
                     "#73c0de", "#3ba272", "#fc8452", "#9a60b4"],
    "legend": {"show": True, "position": "top"},
    "tooltip": {"enabled": True},
    # `label` omitted (Option[String]) so charts show their own column names
    # rather than a hardcoded "X Axis"/"Y Axis".
    "axisLabels": {"x": {"show": True}, "y": {"show": True}},
    "chartType": "line",
}


def _load_pat_env() -> dict[str, str]:
    """HELIO_PAT + HELIO_API_BASE_URL from os.environ, falling back to ./.env."""
    env = {k: os.environ[k] for k in ("HELIO_PAT", "HELIO_API_BASE_URL") if k in os.environ}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    missing = [k for k in ("HELIO_PAT", "HELIO_API_BASE_URL") if k not in env]
    if missing:
        raise RuntimeError(
            f"Missing helio auth env {missing}. Set them in the environment or in "
            f"{_ENV_PATH} (gitignored). The PAT never goes in config or code."
        )
    return env


class HelioClient:
    """Thin async wrapper over the helio MCP tools, with the helpers run.py needs.
    Use via `async with HelioClient.session(config) as helio:`."""

    def __init__(self, session: ClientSession):
        self._s = session

    @classmethod
    @asynccontextmanager
    async def session(cls, config: dict):
        helio = config.get("helio", {})
        params = StdioServerParameters(
            command=os.environ.get("HELIO_MCP_CMD", helio.get("mcp_command", "node")),
            args=helio.get("mcp_args", []),
            env={**os.environ, **_load_pat_env()},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield cls(session)

    async def call(self, tool: str, args: dict | None = None) -> dict | list | str:
        """Call a tool and parse its JSON text result. Raises on tool error."""
        res = await self._s.call_tool(tool, args or {})
        text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        if res.isError:
            raise RuntimeError(f"helio MCP tool {tool} failed: {text}")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    async def tool_names(self) -> set[str]:
        return {t.name for t in (await self._s.list_tools()).tools}

    # ── reads ────────────────────────────────────────────────────────────────
    async def workspace_context(self) -> dict:
        return await self.call("get_workspace_context")

    async def get_dashboard(self, dashboard_id: str) -> dict:
        return await self.call("get_dashboard", {"dashboardId": dashboard_id})

    # ── build a bound data panel from an enricher SourceData ──────────────────
    async def build_bound_panel(self, dashboard_id: str, prefix: str, title: str,
                                sd, background: str = "") -> str:
        """source → single-call pipeline+output → place_outputs. Returns panel id.

        `background`, if given, tints the finished panel (sentiment coloring —
        good news green, bad news red). Applied as a partial appearance patch so
        it does not disturb a chart panel's chart-type appearance.

        HEL-940/HEL-910: panels no longer carry a binding (`dataTypeId`/
        `fieldMapping`/subtype config) — that all lives on the Output now.
        `create_pipeline`'s single call creates the pipeline AND its Output
        (`outputs[]`) in one round trip (replaces the old create_pipeline →
        add_pipeline_step* → run_pipeline → create_panel → bind_panel chain);
        `place_outputs` replaces create_panel + bind_panel for the data-bound
        case."""
        source = await self.call("create_data_source", {
            "name": f"{prefix}-src-{sd.key}",
            "columns": sd.columns,
            "rows": sd.rows,
        })
        source_id = source["id"]
        # Apply the enricher's transform steps (default: an identity select so the
        # pipeline has explicit output columns). A series enricher supplies real
        # steps here — e.g. groupBy month + avg — so helio does the aggregation.
        steps = [
            {"clientId": f"step{i}", "type": step["type"], "config": step.get("config", {})}
            for i, step in enumerate(sd.pipeline_steps())
        ]
        # v1.5 subtype config (collection base/layout, chart display options,
        # table density/order) now lives on the Output's own config, alongside
        # fieldMapping — not on the panel.
        output_config = {**sd.panel_config(), "fieldMapping": sd.mapping}
        output_spec = {
            "kind": sd.panel_type,
            "name": _type_name(prefix, sd.key),
            "config": output_config,
        }
        if steps:
            output_spec["nodeStepClientId"] = steps[-1]["clientId"]
        pipe = await self.call("create_pipeline", {
            "name": f"{prefix}-pipe-{sd.key}",
            "source": {"sourceId": source_id},
            "steps": steps,
            "outputs": [output_spec],
        })
        pipeline_id = pipe["id"]
        await self.call("run_pipeline", {"pipelineId": pipeline_id})
        output_id = pipe["outputs"][0]["id"]

        placed = await self.call("place_outputs", {
            "dashboardId": dashboard_id,
            "items": [{"outputId": output_id, "title": title}],
        })
        panel_id = placed[0]["id"]
        chart_type = sd.chart_type if sd.panel_type == "chart" else None
        await self._apply_appearance(panel_id, chart_type=chart_type,
                                     background=background)
        return panel_id

    async def _apply_appearance(self, panel_id: str, *, chart_type: str | None = None,
                                background: str = "") -> None:
        """Set a panel's chart type and/or background tint in ONE appearance PATCH.

        The single-item appearance PATCH REPLACES the whole PanelAppearance —
        every field the payload omits is reset to its default. Verified against
        the backend: `PanelServiceHelpers.normalizeAppearancePayload` builds a
        fresh `PanelAppearance`, so an omitted `chart` becomes None (dropping a
        chartType) and an omitted `background` becomes the default. chartType and
        the sentiment tint therefore MUST travel together, or the second call
        would wipe the first. `chart` itself must be a COMPLETE ChartAppearance
        (see CHART_APPEARANCE) — a bare {"chartType": …} 400s."""
        appearance: dict = {}
        if chart_type:
            appearance["chart"] = {**CHART_APPEARANCE, "chartType": chart_type}
        if background:
            appearance["background"] = background
        if not appearance:
            return
        await self.call("update_panel_appearance",
                        {"panelId": panel_id, "appearance": appearance})

    async def add_text_panel(self, dashboard_id: str, title: str, content: str,
                             markdown: bool = True) -> str:
        panel = await self.call("create_content_panel", {
            "dashboardId": dashboard_id,
            "type": "markdown" if markdown else "text",
            "title": title,
            "config": {"content": content},
        })
        return panel["id"]

    async def add_image_panel(self, dashboard_id: str, title: str, image_url: str,
                              fit: str = "cover", caption: str = "") -> str:
        """An image panel is unbound content — no source/pipeline needed, the
        browser fetches the URL straight from the outlet's CDN. `fit` ∈
        contain|cover|fill; `cover` fills the panel without letterboxing.
        `caption` (v1.5) renders as a strip beneath the photo — we use it for the
        credit line (headline + outlet)."""
        config: dict = {"imageUrl": image_url, "imageFit": fit}
        if caption:
            config["caption"] = caption
        panel = await self.call("create_content_panel", {
            "dashboardId": dashboard_id,
            "type": "image",
            "title": title,
            "config": config,
        })
        return panel["id"]

    async def create_csv_source(self, name: str, content: str) -> str:
        """Create a CSV data source from inline text. Returns the source id.
        Unlike build_bound_panel's inline `create_data_source` (JSON rows),
        this is for raw tabular data a helio pipeline will aggregate — see
        build_shape_pipeline/build_steps_pipeline."""
        source = await self.call("create_csv_data_source", {"name": name, "content": content})
        return source["id"]

    async def build_shape_pipeline(self, source_id: str, prefix: str, key: str,
                                   shape_id: str, params: dict, *,
                                   output_kind: str = "table") -> str:
        """Instantiate a smart pipeline shape (time-series/single-row/top-n/...)
        against an existing source, run it, return the Output id.

        HEL-940/HEL-910: `create_pipeline_from_shape` was retired — it always
        created a brand-new pipeline. The replacement, `add_outputs_from_shape`,
        expands the shape onto an EXISTING pipeline, so this now creates a
        bare pipeline (no steps/outputs of its own) first, then expands the
        shape onto it, passing `output_kind` so the Output is created with
        the RIGHT kind up front — an Output's `kind` is immutable once
        created (same convention as a panel's `type`), so the caller's
        intended panel kind (chart/metric/table/...) must be supplied here,
        not patched in later via bind_new_panel. Caller still does its own
        place_outputs (see bind_new_panel) — this only builds and runs the
        pipeline, same division of labor as build_bound_panel's chain."""
        pipe = await self.call("create_pipeline", {
            "name": f"{prefix}-pipe-{key}",
            "source": {"sourceId": source_id},
        })
        expanded = await self.call("add_outputs_from_shape", {
            "pipelineId": pipe["id"], "shapeId": shape_id, "params": params,
            "outputName": _type_name(prefix, key), "outputKind": output_kind,
        })
        await self.call("run_pipeline", {"pipelineId": pipe["id"]})
        return expanded["output"]["id"]

    async def build_steps_pipeline(self, source_id: str, prefix: str, key: str,
                                   steps: list[dict], *, output_kind: str = "table") -> str:
        """Build a pipeline from hand-rolled steps, for shapes that don't fit
        (e.g. filter-then-aggregate — no shape combines the two). Returns the
        Output id. HEL-940/HEL-910: single-call create_pipeline now takes
        `steps`/`outputs` inline; `output_kind` is fixed at creation (see
        build_shape_pipeline's docstring for why it can't be patched later)."""
        client_steps = [
            {"clientId": f"step{i}", "type": step["type"], "config": step["config"]}
            for i, step in enumerate(steps)
        ]
        pipe = await self.call("create_pipeline", {
            "name": f"{prefix}-pipe-{key}",
            "source": {"sourceId": source_id},
            "steps": client_steps,
            "outputs": [{
                "kind": output_kind,
                "name": _type_name(prefix, key),
                "nodeStepClientId": client_steps[-1]["clientId"],
            }],
        })
        await self.call("run_pipeline", {"pipelineId": pipe["id"]})
        return pipe["outputs"][0]["id"]

    async def bind_new_panel(self, dashboard_id: str, title: str, panel_type: str,
                             output_id: str, mapping: dict, *,
                             config: dict | None = None, chart_type: str | None = None) -> str:
        """update_output (to set fieldMapping/subtype config) + place_outputs
        (+ appearance for a chart) against an already-run pipeline's Output.
        The tail half of build_bound_panel, for callers (build_shape_pipeline/
        build_steps_pipeline) that built their own pipeline instead of taking
        a SourceData. Returns the panel id. `panel_type` here is retained only
        for the caller's own bookkeeping/chart_type dispatch — it MUST already
        match the Output's `kind` (set at creation via build_shape_pipeline/
        build_steps_pipeline's `output_kind`, since kind is immutable).

        HEL-940/HEL-910: a panel placement carries only `outputId` now —
        `fieldMapping`/subtype `config` live on the Output itself, so this
        PATCHes the Output (update_output) before placing it, rather than
        PATCHing the panel (the old bind_panel)."""
        output_config = {**(config or {}), "fieldMapping": mapping}
        await self.call("update_output", {"outputId": output_id, "config": output_config})
        placed = await self.call("place_outputs", {
            "dashboardId": dashboard_id,
            "items": [{"outputId": output_id, "title": title}],
        })
        panel_id = placed[0]["id"]
        if chart_type:
            await self._apply_appearance(panel_id, chart_type=chart_type)
        return panel_id

    # ── dashboard lifecycle ───────────────────────────────────────────────────
    async def set_layout(self, dashboard_id: str, items: list[dict]) -> None:
        """Position panels on the grid via update_dashboard_layout (no-op if the
        MCP server predates that tool)."""
        if "update_dashboard_layout" not in await self.tool_names():
            return
        await self.call("update_dashboard_layout",
                        {"dashboardId": dashboard_id, "items": items})

    async def ensure_dashboard(self, name: str) -> str:
        page = await self.call("list_dashboards")
        items = page.get("items", []) if isinstance(page, dict) else []
        for d in items:
            if d.get("name") == name:
                return d["id"]
        return (await self.call("create_dashboard", {"name": name}))["id"]

    async def clear_dashboard_panels(self, dashboard_id: str) -> int:
        dash = await self.get_dashboard(dashboard_id)
        panels = dash.get("panels", []) if isinstance(dash, dict) else []
        for p in panels:
            # export snapshots expose the panel id as `snapshotId`.
            pid = p.get("snapshotId") or p.get("id")
            if pid:
                await self.call("delete_panel", {"panelId": pid})
        return len(panels)

    async def cleanup_news_resources(self, type_prefix: str = "news_out_") -> dict:
        """Delete previous runs' news pipelines (cascades their Outputs) and
        sources. Matches any `news-*…-src-*` source, and any pipeline whose
        name starts with `news-` or produced a `news_out_*`-prefixed Output,
        so it also sweeps ad-hoc/probe resources. Idempotent.

        HEL-940/HEL-910: the DataType model (and `delete_data_type`) was
        retired outright by HEL-904 — a pipeline's Outputs are deleted by
        deleting the pipeline (`delete_pipeline` cascades its Outputs), not
        as a separate DataType-delete pass. `list_outputs`/`workspace_context`
        no longer carry a `dataTypes` field.

        Best-effort per resource: a source/pipeline a panel *outside* today's
        boards still binds to (a stray manual/scratch dashboard, say) 409s on
        delete — that must not abort cleanup for the rest of the workspace,
        since the board-building work this run already did is real and worth
        keeping."""
        deleted = {"sources": 0, "pipelines": 0}
        ctx = await self.workspace_context()
        for p in ctx.get("pipelines", []):
            name = p.get("name", "")
            outputs = p.get("outputs", []) or []
            matches_output = any(
                (o.get("name") or "").startswith(type_prefix) for o in outputs
            )
            if name.startswith("news-") or matches_output:
                try:
                    await self.call("delete_pipeline", {"pipelineId": p["id"]})
                    deleted["pipelines"] += 1
                except RuntimeError as e:
                    print(f"· cleanup: skipped pipeline {p['id']} ({name}): {e}",
                          file=sys.stderr)
        # Re-read: deleting a pipeline does not delete its source. Sweep any
        # `news-*…-src-*` source separately (covers sources whose pipeline
        # already failed to create, or was deleted in a prior partial run).
        ctx = await self.workspace_context()
        for s in ctx.get("dataSources", []):
            name = s.get("name", "")
            if name.startswith("news-") and "-src-" in name:
                try:
                    await self.call("delete_data_source", {"dataSourceId": s["id"]})
                    deleted["sources"] += 1
                except RuntimeError as e:
                    print(f"· cleanup: skipped source {s['id']} ({name}): {e}",
                          file=sys.stderr)
        return deleted


def _type_name(prefix: str, key: str) -> str:
    """helio DataType name — identifier-ish, `news_out_`-prefixed for cleanup, and
    stamped with the board prefix so the same data key (e.g. a ticker) built on two
    different boards doesn't collide on one shared output-type name."""
    stem = f"{prefix}-{key}".replace("news-", "", 1).replace("-", "_")
    return "news_out_" + stem
