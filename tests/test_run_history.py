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
