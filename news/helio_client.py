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
        """source → trivial pipeline → run → panel → bind. Returns panel id.

        `background`, if given, tints the finished panel (sentiment coloring —
        good news green, bad news red). Applied as a partial appearance patch so
        it does not disturb a chart panel's chart-type appearance."""
        source = await self.call("create_data_source", {
            "name": f"{prefix}-src-{sd.key}",
            "columns": sd.columns,
            "rows": sd.rows,
        })
        source_id = source["id"]
        pipe = await self.call("create_pipeline", {
            "name": f"{prefix}-pipe-{sd.key}",
            "sourceDataSourceId": source_id,
            "outputDataTypeName": _type_name(prefix, sd.key),
        })
        pipeline_id = pipe["id"]
        # Identity select so the pipeline has explicit output columns, then run.
        await self.call("add_pipeline_step", {
            "pipelineId": pipeline_id, "type": "select",
            "config": {"fields": sd.column_names()},
        })
        run = await self.call("run_pipeline", {"pipelineId": pipeline_id})
        output_type_id = run["outputDataTypeId"]

        # v1.5 subtype config (collection base/layout, chart display options +
        # annotation, table density/order) travels at create time; bind_panel
        # merge-patches the binding on top without disturbing it.
        create_args = {"dashboardId": dashboard_id, "type": sd.panel_type, "title": title}
        config = sd.panel_config()
        if config:
            create_args["config"] = config
        panel = await self.call("create_panel", create_args)
        panel_id = panel["id"]
        await self.call("bind_panel", {
            "panelId": panel_id, "dataTypeId": output_type_id,
            "fieldMapping": sd.mapping, "panelType": sd.panel_type,
        })
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
        panel = await self.call("create_panel", {
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
        panel = await self.call("create_panel", {
            "dashboardId": dashboard_id,
            "type": "image",
            "title": title,
            "config": config,
        })
        return panel["id"]

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
        """Delete previous runs' news sources (cascades their pipelines) and the
        orphaned pipeline-output DataTypes. Matches any `news-*…-src-*` source so
        it also sweeps ad-hoc/probe resources. Idempotent."""
        deleted = {"sources": 0, "types": 0}
        ctx = await self.workspace_context()
        for s in ctx.get("dataSources", []):
            name = s.get("name", "")
            if name.startswith("news-") and "-src-" in name:
                await self.call("delete_data_source", {"dataSourceId": s["id"]})
                deleted["sources"] += 1
        # Re-read: deleting a source orphans its companion DataType (sourceId
        # cleared, not deleted). Remove BOTH our pipeline-output types
        # (`news_out_*`) and those orphaned companions (`news-*-src-*`).
        ctx = await self.workspace_context()
        for t in ctx.get("dataTypes", []):
            name = t.get("name", "")
            if name.startswith(type_prefix) or (name.startswith("news-") and "-src-" in name):
                await self.call("delete_data_type", {"dataTypeId": t["id"]})
                deleted["types"] += 1
        return deleted


def _type_name(prefix: str, key: str) -> str:
    """helio DataType name — identifier-ish, `news_out_`-prefixed for cleanup, and
    stamped with the board prefix so the same data key (e.g. a ticker) built on two
    different boards doesn't collide on one shared output-type name."""
    stem = f"{prefix}-{key}".replace("news-", "", 1).replace("-", "_")
    return "news_out_" + stem
