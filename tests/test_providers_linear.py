"""Linear provider — pagination across the GraphQL page cap.

`first:` on a Linear GraphQL connection is a PAGE size, not a result limit.
The original queries asked for 250 and used whatever came back, so any team
with more than 250 matching tickets had its oldest silently dropped — and the
project-pulse cycle-time/backlog-age metrics were then computed on a truncated
set. These tests pin the cursor walk.
"""

import news.providers.linear as linear


def _page(nodes, has_next, cursor=None):
    return {"data": {"issues": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }}}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install(monkeypatch, pages):
    """Serve `pages` in order; record the variables each request carried."""
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append(json["variables"])
        return _Resp(pages[len(seen) - 1])

    monkeypatch.setattr(linear.requests, "post", fake_post)
    monkeypatch.setattr(linear, "_api_key", lambda: "lin_api_test")
    return seen


def test_follows_the_cursor_until_the_last_page(monkeypatch):
    seen = _install(monkeypatch, [
        _page([{"identifier": f"A-{i}"} for i in range(250)], True, "cursor-1"),
        _page([{"identifier": f"B-{i}"} for i in range(250)], True, "cursor-2"),
        _page([{"identifier": f"C-{i}"} for i in range(7)], False, None),
    ])

    rows = linear.fetch_open("Helio Platform")

    assert len(rows) == 507
    assert rows[0]["identifier"] == "A-0"
    assert rows[-1]["identifier"] == "C-6"
    assert [v.get("after") for v in seen] == [None, "cursor-1", "cursor-2"]


def test_single_page_makes_exactly_one_request(monkeypatch):
    seen = _install(monkeypatch, [_page([{"identifier": "A-1"}], False, None)])

    rows = linear.fetch_completed("Helio Platform", 90)

    assert len(rows) == 1
    assert len(seen) == 1
    assert seen[0]["teamName"] == "Helio Platform"
    assert seen[0]["since"] == "-P90D"


def test_stops_at_the_page_budget_and_warns(monkeypatch, capsys):
    """A team that never stops paging must not loop forever — bound the walk
    and say so, rather than silently truncating the way the old cap did."""
    pages = [_page([{"identifier": f"X-{i}"} for i in range(250)], True, f"c{i}")
             for i in range(linear.MAX_PAGES + 1)]
    _install(monkeypatch, pages)

    rows = linear.fetch_open("Helio Platform")

    assert len(rows) == 250 * linear.MAX_PAGES
    assert "page budget" in capsys.readouterr().err


def test_missing_key_still_returns_none(monkeypatch):
    monkeypatch.setattr(linear, "_api_key", lambda: None)
    monkeypatch.setattr(linear, "_warned", False)
    assert linear.fetch_open("Helio Platform") is None


def test_graphql_errors_still_propagate(monkeypatch):
    monkeypatch.setattr(linear, "_api_key", lambda: "lin_api_test")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp({"errors": [{"message": "boom"}]})

    monkeypatch.setattr(linear.requests, "post", fake_post)
    try:
        linear.fetch_open("Helio Platform")
    except RuntimeError as e:
        assert "Linear API error" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
