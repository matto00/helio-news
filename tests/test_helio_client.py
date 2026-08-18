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
