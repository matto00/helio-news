from types import SimpleNamespace
from unittest.mock import patch

from news import run


def _story(summary="The Fed cut rates.", articles=None, continuity=None):
    return SimpleNamespace(summary=summary, headline="Fed cuts rates",
                           _articles=articles or [], _continuity=continuity)


# ── story_markdown ───────────────────────────────────────────────────────────

def test_story_markdown_appends_continuity_note_when_present():
    continuity = {"note": "Fourth consecutive day of coverage."}
    md = run.story_markdown(_story(continuity=continuity))
    assert md.endswith("*Fourth consecutive day of coverage.*")


def test_story_markdown_omits_continuity_block_when_absent():
    md = run.story_markdown(_story(continuity=None))
    assert "*" not in md


def test_story_markdown_omits_continuity_block_when_note_empty():
    md = run.story_markdown(_story(continuity={"note": ""}))
    assert "*" not in md


# ── curator fatigue signal ───────────────────────────────────────────────────

def test_continuity_brief_fields_present():
    story = _story(continuity={"days_running": 4, "trend": "rising", "note": "x"})
    assert run._continuity_brief_fields(story) == {"days_running": 4, "trend": "rising"}


def test_continuity_brief_fields_defaults_when_absent():
    story = _story(continuity=None)
    assert run._continuity_brief_fields(story) == {"days_running": 0, "trend": ""}


# ── write-gating: history is written on a real run, never --plan-only ───────

def test_plan_only_never_writes_history():
    from datetime import date
    fake_plan = SimpleNamespace(day=date(2026, 8, 9), stories=[])
    with patch.object(run, "build_plan", return_value=(fake_plan, [], {})), \
         patch.object(run, "history_write_day") as write_mock, \
         patch.object(run, "load_config", return_value={}):
        run.main(["--plan-only"])
    write_mock.assert_not_called()


def test_real_run_writes_history_before_apply_plan():
    fake_story = SimpleNamespace(slug="s", headline="h", subject="", domain="general",
                                 importance=3, breaking=False, sentiment="neutral",
                                 summary="", _articles=[])
    from datetime import date
    fake_plan = SimpleNamespace(day=date(2026, 8, 9), stories=[fake_story])
    with patch.object(run, "build_plan", return_value=(fake_plan, [], {})), \
         patch.object(run, "history_write_day") as write_mock, \
         patch.object(run, "apply_plan", new=lambda *a, **kw: None), \
         patch("asyncio.run"), \
         patch.object(run, "load_config", return_value={}):
        run.main([])
    write_mock.assert_called_once()
    called_day, called_entries, _retention = write_mock.call_args[0]
    assert called_day == date(2026, 8, 9)
    assert called_entries[0].slug == "s"


# ── history write failure must never abort the dashboard build ──────────────

def test_history_write_failure_is_non_fatal():
    fake_story = SimpleNamespace(slug="s", headline="h", subject="", domain="general",
                                 importance=3, breaking=False, sentiment="neutral",
                                 summary="", _articles=[])
    from datetime import date
    fake_plan = SimpleNamespace(day=date(2026, 8, 9), stories=[fake_story])
    with patch.object(run, "build_plan", return_value=(fake_plan, [], {})), \
         patch.object(run, "history_write_day", side_effect=OSError("disk full")), \
         patch.object(run, "apply_plan", new=lambda *a, **kw: None), \
         patch("asyncio.run") as asyncio_mock, \
         patch.object(run, "load_config", return_value={}):
        result = run.main([])
    assert result == 0
    asyncio_mock.assert_called_once()


# ── project boards folded into apply_plan ────────────────────────────────────

def test_apply_plan_folds_project_names_into_board_ids_and_calls_build_project_boards():
    import asyncio
    from datetime import date

    config = {
        "dashboards": {"overview": "News Overview", "sections": {"Tech & AI": ["tech", "ai"]}},
        "projects": {"enabled": True, "items": [{"name": "Helio", "linear_team": "Helio Platform",
                                                  "repo_path": "/repo/helio"}]},
    }
    fake_plan = SimpleNamespace(day=date(2026, 8, 18), stories=[],
                                resource_prefix=lambda: "news")

    class _FakeHelio:
        def __init__(self):
            self.ensured = []
            self.cleared = []

        async def tool_names(self):
            return {"delete_data_source", "delete_dashboard", "delete_data_type", "delete_panel"}

        async def ensure_dashboard(self, name):
            self.ensured.append(name)
            return f"dash-{name}"

        async def clear_dashboard_panels(self, dashboard_id):
            self.cleared.append(dashboard_id)
            return 0

        async def cleanup_news_resources(self):
            return {"sources": 0, "types": 0}

    fake_helio = _FakeHelio()

    class _FakeSession:
        async def __aenter__(self):
            return fake_helio

        async def __aexit__(self, *exc):
            return False

    # apply_plan's day-in-review loop calls briefing.recap(plan, config) even
    # with zero stories/articles, and recap() reads real state/history/*.json
    # files from disk — patch it out so this test doesn't depend on (or
    # break on changes to) this repo's real history data, and doesn't need
    # _FakeHelio to implement build_bound_panel/set_layout for a panel this
    # test isn't about.
    with patch.object(run.HelioClient, "session", return_value=_FakeSession()), \
         patch("news.run.briefing.recap", return_value=None), \
         patch("news.run.build_project_boards") as build_mock:
        asyncio.run(run.apply_plan(fake_plan, [], config, {}, cleanup=True))

    assert "Helio" in fake_helio.ensured
    assert "dash-Helio" in fake_helio.cleared
    build_mock.assert_called_once()
    call_args = build_mock.call_args.args
    assert call_args[0] is config
    assert call_args[1] is fake_helio
    assert call_args[2]["Helio"] == "dash-Helio"


def test_apply_plan_skips_project_items_missing_name():
    """A projects.items entry missing 'name' (a config typo) must not raise
    KeyError before HelioClient.session even opens — that would take down
    the whole daily run, news boards included, not just project boards."""
    import asyncio
    from datetime import date

    config = {
        "dashboards": {"overview": "News Overview", "sections": {}},
        "projects": {"enabled": True, "items": [
            {"linear_team": "Helio Platform", "repo_path": "/repo/helio"},  # no "name"
            {"name": "Concertino", "linear_team": "Concertino", "repo_path": "/repo/concertino"},
        ]},
    }
    fake_plan = SimpleNamespace(day=date(2026, 8, 18), stories=[],
                                resource_prefix=lambda: "news")

    class _FakeHelio:
        def __init__(self):
            self.ensured = []

        async def tool_names(self):
            return {"delete_data_source", "delete_dashboard", "delete_data_type", "delete_panel"}

        async def ensure_dashboard(self, name):
            self.ensured.append(name)
            return f"dash-{name}"

        async def clear_dashboard_panels(self, dashboard_id):
            return 0

        async def cleanup_news_resources(self):
            return {"sources": 0, "types": 0}

    fake_helio = _FakeHelio()

    class _FakeSession:
        async def __aenter__(self):
            return fake_helio

        async def __aexit__(self, *exc):
            return False

    with patch.object(run.HelioClient, "session", return_value=_FakeSession()), \
         patch("news.run.briefing.recap", return_value=None), \
         patch("news.run.build_project_boards") as build_mock:
        asyncio.run(run.apply_plan(fake_plan, [], config, {}, cleanup=True))

    # the unnamed item never got a board; Concertino did
    assert "Concertino" in fake_helio.ensured
    assert not any(name == "" for name in fake_helio.ensured)
    call_args = build_mock.call_args.args
    assert call_args[2] == {"News Overview": "dash-News Overview",
                            "Concertino": "dash-Concertino"}


def test_apply_plan_calls_build_project_boards_immediately_after_cleanup():
    """Project-pulse boards must build right after the shared cleanup pass,
    not at the very end of apply_plan — otherwise a later failure in
    news-board-building leaves the already-cleared project boards blank for
    the whole day even though project-pulse never touches news data."""
    import asyncio
    from datetime import date

    config = {
        "dashboards": {"overview": "News Overview", "sections": {"Tech & AI": ["tech", "ai"]}},
        "projects": {"enabled": True, "items": [{"name": "Helio", "linear_team": "Helio Platform",
                                                  "repo_path": "/repo/helio"}]},
    }
    fake_plan = SimpleNamespace(day=date(2026, 8, 18), stories=[],
                                resource_prefix=lambda: "news")
    calls: list[str] = []

    class _FakeHelio:
        async def tool_names(self):
            return {"delete_data_source", "delete_dashboard", "delete_data_type", "delete_panel"}

        async def ensure_dashboard(self, name):
            return f"dash-{name}"

        async def clear_dashboard_panels(self, dashboard_id):
            return 0

        async def cleanup_news_resources(self):
            calls.append("cleanup")
            return {"sources": 0, "types": 0}

    fake_helio = _FakeHelio()

    class _FakeSession:
        async def __aenter__(self):
            return fake_helio

        async def __aexit__(self, *exc):
            return False

    def _record_build(*a, **kw):
        calls.append("build_project_boards")

    with patch.object(run.HelioClient, "session", return_value=_FakeSession()), \
         patch("news.run.briefing.recap", side_effect=lambda *a, **kw: (calls.append("recap"), None)[1]), \
         patch("news.run.build_project_boards", side_effect=_record_build):
        asyncio.run(run.apply_plan(fake_plan, [], config, {}, cleanup=True))

    # build_project_boards must run right after cleanup, before the
    # news-board-building code (represented here by the day-in-review recap call)
    assert calls.index("build_project_boards") < calls.index("recap")
    assert calls.index("cleanup") < calls.index("build_project_boards")
