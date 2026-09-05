import asyncio

from news.helio_client import HelioClient


class _StubCalls:
    """Records every HelioClient.call(...) and canned-responds per tool name,
    raising RuntimeError (mirroring HelioClient.call's isError path) for calls
    matching an entry in `failures`."""

    def __init__(self, responses, failures=None):
        self.responses = responses
        self.failures = failures or {}
        self.calls = []

    async def __call__(self, tool, args=None):
        args = args or {}
        self.calls.append((tool, args))
        key = (tool, args.get("pipelineId") or args.get("dataSourceId"))
        if key in self.failures:
            raise RuntimeError(self.failures[key])
        return self.responses.get(tool, {})


def _client(stub: _StubCalls) -> HelioClient:
    client = HelioClient.__new__(HelioClient)
    client.call = stub
    return client


def test_cleanup_news_resources_skips_pipeline_still_bound_to_a_panel():
    """A pipeline a stray/foreign panel still binds an Output to 409s on
    delete — that must not abort cleanup for the rest of the workspace or
    fail the run."""
    ctx = {
        "dataSources": [{"id": "src-1", "name": "news-overview-src-foo"}],
        "pipelines": [
            {"id": "pipe-1", "name": "news-overview-pipe-foo", "outputs": []},
            {"id": "pipe-2", "name": "other-pipe", "outputs": [{"name": "news_out_bar"}]},
        ],
    }
    stub = _StubCalls(
        responses={"get_workspace_context": ctx},
        failures={
            ("delete_pipeline", "pipe-1"): (
                "409: Cannot delete pipeline: one or more panels are bound to its Output"
            ),
        },
    )
    client = _client(stub)

    result = asyncio.run(client.cleanup_news_resources())

    assert result == {"sources": 1, "pipelines": 1}
    deleted_pipelines = [a["pipelineId"] for t, a in stub.calls if t == "delete_pipeline"]
    assert deleted_pipelines == ["pipe-1", "pipe-2"]  # both attempted; only pipe-2 counted


def test_cleanup_news_resources_skips_source_that_fails_to_delete():
    ctx = {
        "dataSources": [
            {"id": "src-1", "name": "news-overview-src-foo"},
            {"id": "src-2", "name": "news-overview-src-bar"},
        ],
        "pipelines": [],
    }
    stub = _StubCalls(
        responses={"get_workspace_context": ctx},
        failures={("delete_data_source", "src-1"): "409: still referenced"},
    )
    client = _client(stub)

    result = asyncio.run(client.cleanup_news_resources())

    assert result == {"sources": 1, "pipelines": 0}
    deleted_sources = [a["dataSourceId"] for t, a in stub.calls if t == "delete_data_source"]
    assert deleted_sources == ["src-1", "src-2"]  # both attempted; only src-2 counted


def test_cleanup_news_resources_all_succeed():
    ctx = {
        "dataSources": [{"id": "src-1", "name": "news-overview-src-foo"}],
        "pipelines": [{"id": "pipe-1", "name": "news-overview-pipe-foo", "outputs": []}],
    }
    stub = _StubCalls(responses={"get_workspace_context": ctx})
    client = _client(stub)

    result = asyncio.run(client.cleanup_news_resources())

    assert result == {"sources": 1, "pipelines": 1}


def test_create_csv_source_returns_id():
    stub = _StubCalls(responses={"create_csv_data_source": {"id": "src-99"}})
    client = _client(stub)

    result = asyncio.run(client.create_csv_source("news-proj-helio-src-completed", "id,title\n1,A\n"))

    assert result == "src-99"
    assert stub.calls == [("create_csv_data_source",
                            {"name": "news-proj-helio-src-completed", "content": "id,title\n1,A\n"})]


def test_build_shape_pipeline_creates_expands_runs_and_returns_output_id():
    stub = _StubCalls(responses={
        "create_pipeline": {"id": "pipe-1"},
        "add_outputs_from_shape": {"output": {"id": "output-1"}},
    })
    client = _client(stub)

    result = asyncio.run(client.build_shape_pipeline(
        "src-99", "news-proj-helio", "velocity", "time-series",
        {"timeField": "completedAt", "granularity": "week",
         "measures": [{"fn": "count", "field": "id", "alias": "ticketsCompleted"}]},
        output_kind="chart"))

    assert result == "output-1"
    create_call = next(c for t, c in stub.calls if t == "create_pipeline")
    assert create_call["roots"] == [{"sourceId": "src-99"}]
    shape_call = next(c for t, c in stub.calls if t == "add_outputs_from_shape")
    assert shape_call["pipelineId"] == "pipe-1"
    assert shape_call["shapeId"] == "time-series"
    assert shape_call["outputName"] == "news_out_proj_helio_velocity"
    assert shape_call["outputKind"] == "chart"
    assert shape_call["params"]["timeField"] == "completedAt"
    run_call = next(c for t, c in stub.calls if t == "run_pipeline")
    assert run_call == {"pipelineId": "pipe-1"}


def test_build_steps_pipeline_creates_with_inline_steps_and_returns_output_id():
    stub = _StubCalls(responses={
        "create_pipeline": {"id": "pipe-2", "outputs": [{"id": "output-2"}]},
    })
    client = _client(stub)
    steps = [
        {"type": "filter", "config": {"combinator": "AND",
                                       "conditions": [{"field": "isBug", "operator": "=", "value": "true"}]}},
        {"type": "aggregate", "config": {"groupBy": [],
                                          "aggregations": [{"alias": "openBugCount", "field": "id", "fn": "count"}]}},
    ]

    result = asyncio.run(client.build_steps_pipeline(
        "src-100", "news-proj-helio", "openbugs", steps, output_kind="metric"))

    assert result == "output-2"
    create_call = next(c for t, c in stub.calls if t == "create_pipeline")
    assert create_call["roots"] == [{"sourceId": "src-100"}]
    assert create_call["steps"] == [
        {"clientId": "step0", "type": "filter", "config": steps[0]["config"]},
        {"clientId": "step1", "type": "aggregate", "config": steps[1]["config"],
         "parentStepId": "step0"},
    ]
    assert create_call["outputs"] == [{
        "kind": "metric", "name": "news_out_proj_helio_openbugs", "nodeStepClientId": "step1",
    }]
    run_call = next(c for t, c in stub.calls if t == "run_pipeline")
    assert run_call == {"pipelineId": "pipe-2"}


def test_bind_new_panel_metric_no_appearance_call():
    stub = _StubCalls(responses={"place_outputs": [{"id": "panel-1"}]})
    client = _client(stub)

    result = asyncio.run(client.bind_new_panel(
        "dash-1", "Avg Cycle Time", "metric", "output-1", {"value": "avgCycleTimeDays"}))

    assert result == "panel-1"
    update_call = next(c for t, c in stub.calls if t == "update_output")
    assert update_call == {"outputId": "output-1",
                            "config": {"fieldMapping": {"value": "avgCycleTimeDays"}}}
    place_call = next(c for t, c in stub.calls if t == "place_outputs")
    assert place_call == {"dashboardId": "dash-1",
                           "items": [{"outputId": "output-1", "title": "Avg Cycle Time"}]}
    assert not any(t == "update_panel_appearance" for t, _ in stub.calls)


def test_bind_new_panel_chart_applies_appearance():
    stub = _StubCalls(responses={"place_outputs": [{"id": "panel-2"}]})
    client = _client(stub)

    asyncio.run(client.bind_new_panel(
        "dash-1", "Velocity", "chart", "output-2",
        {"xAxis": "completedAt", "yAxis": "ticketsCompleted"}, chart_type="bar"))

    appearance_call = next(c for t, c in stub.calls if t == "update_panel_appearance")
    assert appearance_call["panelId"] == "panel-2"
    assert appearance_call["appearance"]["chart"]["chartType"] == "bar"


def test_build_bound_panel_sends_roots_array_and_chains_multi_step_transforms():
    """HEL-913/HEL-914 regression guard. `create_pipeline` takes a non-empty
    `roots[]` (the scalar `source` was removed outright — the server's zod
    schema is `additionalProperties: false`, so the old shape is rejected), and
    a parentless step hangs off the ROOT, not off the previous array element.
    The monthly-series enricher's aggregate → sort is the real case: unchained,
    `sort` would sort the raw rows and the Output would never see `avg_value`."""

    class _SD:
        key = "cpi"
        columns = [{"name": "month", "type": "string"}, {"name": "value", "type": "number"}]
        rows = [["2026-01", 1.0]]
        mapping = {"xAxis": "month", "yAxis": "avg_value"}
        panel_type = "chart"
        chart_type = "line"

        def pipeline_steps(self):
            return [
                {"type": "aggregate", "config": {"groupBy": [{"name": "month", "type": "string"}]}},
                {"type": "sort", "config": {"sortBy": [{"field": "month", "direction": "asc"}]}},
            ]

        def panel_config(self):
            return {}

    stub = _StubCalls(responses={
        "create_data_source": {"id": "src-7"},
        "create_pipeline": {"id": "pipe-7", "outputs": [{"id": "output-7"}]},
        "place_outputs": [{"id": "panel-7"}],
    })
    client = _client(stub)

    panel_id = asyncio.run(client.build_bound_panel("dash-1", "news-overview", "CPI", _SD()))

    assert panel_id == "panel-7"
    create_call = next(c for t, c in stub.calls if t == "create_pipeline")
    assert "source" not in create_call
    assert create_call["roots"] == [{"sourceId": "src-7"}]
    assert create_call["steps"] == [
        {"clientId": "step0", "type": "aggregate",
         "config": {"groupBy": [{"name": "month", "type": "string"}]}},
        {"clientId": "step1", "type": "sort", "parentStepId": "step0",
         "config": {"sortBy": [{"field": "month", "direction": "asc"}]}},
    ]
    assert create_call["outputs"][0]["nodeStepClientId"] == "step1"
