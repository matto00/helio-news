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
        key = (tool, args.get("dataTypeId") or args.get("dataSourceId"))
        if key in self.failures:
            raise RuntimeError(self.failures[key])
        return self.responses.get(tool, {})


def _client(stub: _StubCalls) -> HelioClient:
    client = HelioClient.__new__(HelioClient)
    client.call = stub
    return client


def test_cleanup_news_resources_skips_type_still_bound_to_a_panel():
    """A DataType a stray/foreign panel still binds to 409s on delete — that
    must not abort cleanup for the rest of the workspace or fail the run."""
    ctx = {
        "dataSources": [{"id": "src-1", "name": "news-overview-src-foo"}],
        "dataTypes": [
            {"id": "type-1", "name": "news_out_foo"},
            {"id": "type-2", "name": "news_out_bar"},
        ],
    }
    stub = _StubCalls(
        responses={"get_workspace_context": ctx},
        failures={
            ("delete_data_type", "type-1"): (
                "409: Cannot delete DataType: one or more panels are bound to it"
            ),
        },
    )
    client = _client(stub)

    result = asyncio.run(client.cleanup_news_resources())

    assert result == {"sources": 1, "types": 1}
    deleted_types = [a["dataTypeId"] for t, a in stub.calls if t == "delete_data_type"]
    assert deleted_types == ["type-1", "type-2"]  # both attempted; only type-2 counted


def test_cleanup_news_resources_skips_source_that_fails_to_delete():
    ctx = {
        "dataSources": [
            {"id": "src-1", "name": "news-overview-src-foo"},
            {"id": "src-2", "name": "news-overview-src-bar"},
        ],
        "dataTypes": [],
    }
    stub = _StubCalls(
        responses={"get_workspace_context": ctx},
        failures={("delete_data_source", "src-1"): "409: still referenced"},
    )
    client = _client(stub)

    result = asyncio.run(client.cleanup_news_resources())

    assert result == {"sources": 1, "types": 0}
    deleted_sources = [a["dataSourceId"] for t, a in stub.calls if t == "delete_data_source"]
    assert deleted_sources == ["src-1", "src-2"]  # both attempted; only src-2 counted


def test_cleanup_news_resources_all_succeed():
    ctx = {
        "dataSources": [{"id": "src-1", "name": "news-overview-src-foo"}],
        "dataTypes": [{"id": "type-1", "name": "news_out_foo"}],
    }
    stub = _StubCalls(responses={"get_workspace_context": ctx})
    client = _client(stub)

    result = asyncio.run(client.cleanup_news_resources())

    assert result == {"sources": 1, "types": 1}


def test_create_csv_source_returns_id():
    stub = _StubCalls(responses={"create_csv_data_source": {"id": "src-99"}})
    client = _client(stub)

    result = asyncio.run(client.create_csv_source("news-proj-helio-src-completed", "id,title\n1,A\n"))

    assert result == "src-99"
    assert stub.calls == [("create_csv_data_source",
                            {"name": "news-proj-helio-src-completed", "content": "id,title\n1,A\n"})]


def test_build_shape_pipeline_creates_runs_and_returns_output_type():
    stub = _StubCalls(responses={
        "create_pipeline_from_shape": {"id": "pipe-1"},
        "run_pipeline": {"outputDataTypeId": "type-1"},
    })
    client = _client(stub)

    result = asyncio.run(client.build_shape_pipeline(
        "src-99", "news-proj-helio", "velocity", "time-series",
        {"timeField": "completedAt", "granularity": "week",
         "measures": [{"fn": "count", "field": "id", "alias": "ticketsCompleted"}]}))

    assert result == "type-1"
    shape_call = next(c for t, c in stub.calls if t == "create_pipeline_from_shape")
    assert shape_call["shapeId"] == "time-series"
    assert shape_call["sourceDataSourceId"] == "src-99"
    assert shape_call["outputDataTypeName"] == "news_out_proj_helio_velocity"
    assert shape_call["params"]["timeField"] == "completedAt"
    run_call = next(c for t, c in stub.calls if t == "run_pipeline")
    assert run_call == {"pipelineId": "pipe-1"}


def test_build_steps_pipeline_creates_adds_steps_runs_and_returns_output_type():
    stub = _StubCalls(responses={
        "create_pipeline": {"id": "pipe-2"},
        "run_pipeline": {"outputDataTypeId": "type-2"},
    })
    client = _client(stub)
    steps = [
        {"type": "filter", "config": {"combinator": "AND",
                                       "conditions": [{"field": "isBug", "operator": "=", "value": "true"}]}},
        {"type": "aggregate", "config": {"groupBy": [],
                                          "aggregations": [{"alias": "openBugCount", "field": "id", "fn": "count"}]}},
    ]

    result = asyncio.run(client.build_steps_pipeline("src-100", "news-proj-helio", "openbugs", steps))

    assert result == "type-2"
    create_call = next(c for t, c in stub.calls if t == "create_pipeline")
    assert create_call["sourceDataSourceId"] == "src-100"
    assert create_call["outputDataTypeName"] == "news_out_proj_helio_openbugs"
    step_calls = [c for t, c in stub.calls if t == "add_pipeline_step"]
    assert len(step_calls) == 2
    assert step_calls[0] == {"pipelineId": "pipe-2", "type": "filter", "config": steps[0]["config"]}
    assert step_calls[1] == {"pipelineId": "pipe-2", "type": "aggregate", "config": steps[1]["config"]}
    run_call = next(c for t, c in stub.calls if t == "run_pipeline")
    assert run_call == {"pipelineId": "pipe-2"}


def test_bind_new_panel_metric_no_appearance_call():
    stub = _StubCalls(responses={"create_panel": {"id": "panel-1"}})
    client = _client(stub)

    result = asyncio.run(client.bind_new_panel(
        "dash-1", "Avg Cycle Time", "metric", "type-1", {"value": "avgCycleTimeDays"}))

    assert result == "panel-1"
    create_call = next(c for t, c in stub.calls if t == "create_panel")
    assert create_call == {"dashboardId": "dash-1", "type": "metric", "title": "Avg Cycle Time"}
    bind_call = next(c for t, c in stub.calls if t == "bind_panel")
    assert bind_call == {"panelId": "panel-1", "dataTypeId": "type-1",
                          "fieldMapping": {"value": "avgCycleTimeDays"}, "panelType": "metric"}
    assert not any(t == "update_panel_appearance" for t, _ in stub.calls)


def test_bind_new_panel_chart_applies_appearance():
    stub = _StubCalls(responses={"create_panel": {"id": "panel-2"}})
    client = _client(stub)

    asyncio.run(client.bind_new_panel(
        "dash-1", "Velocity", "chart", "type-2",
        {"xAxis": "completedAt", "yAxis": "ticketsCompleted"}, chart_type="bar"))

    appearance_call = next(c for t, c in stub.calls if t == "update_panel_appearance")
    assert appearance_call["panelId"] == "panel-2"
    assert appearance_call["appearance"]["chart"]["chartType"] == "bar"
