import asyncio
from unittest.mock import patch

from news.projects import build


def _config():
    return {
        "projects": {
            "enabled": True, "lookback_days": 90, "narrative_days": 7, "backlog_top_n": 5,
            "items": [
                {"name": "Helio", "linear_team": "Helio Platform", "repo_path": "/repo/helio"},
                {"name": "Concertino", "linear_team": "Concertino", "repo_path": "/repo/concertino"},
            ],
        },
        "models": {"projects_summary": "gpt-oss:latest"},
        "reasoning": {"projects_summary": "medium"},
        "ollama": {},
    }


class _FakeHelio:
    def __init__(self):
        self.csv_sources_created = []
        self.shape_pipelines_built = []
        self.steps_pipelines_built = []
        self.panels_bound = []
        self.text_panels_added = []
        self.layouts_set = []

    async def create_csv_source(self, name, content):
        self.csv_sources_created.append((name, content))
        return f"src-{len(self.csv_sources_created)}"

    async def build_shape_pipeline(self, source_id, prefix, key, shape_id, params):
        self.shape_pipelines_built.append((source_id, prefix, key, shape_id, params))
        return f"type-shape-{len(self.shape_pipelines_built)}"

    async def build_steps_pipeline(self, source_id, prefix, key, steps):
        self.steps_pipelines_built.append((source_id, prefix, key, steps))
        return f"type-steps-{len(self.steps_pipelines_built)}"

    async def bind_new_panel(self, dashboard_id, title, panel_type, output_type_id, mapping,
                             *, config=None, chart_type=None):
        panel_id = f"panel-{len(self.panels_bound) + 1}"
        self.panels_bound.append((dashboard_id, title, panel_type, output_type_id, mapping, chart_type, panel_id))
        return panel_id

    async def add_text_panel(self, dashboard_id, title, content, markdown=True):
        panel_id = f"panel-text-{len(self.text_panels_added) + 1}"
        self.text_panels_added.append((dashboard_id, title, content, panel_id))
        return panel_id

    async def set_layout(self, dashboard_id, items):
        self.layouts_set.append((dashboard_id, items))


_COMPLETED = [{"identifier": "HEL-1", "title": "Ship the thing", "priority": 2,
              "createdAt": "2026-08-01T00:00:00.000Z", "startedAt": "2026-08-01T00:00:00.000Z",
              "completedAt": "2026-08-03T00:00:00.000Z", "labels": {"nodes": []}}]
_OPEN = [{"identifier": "HEL-2", "title": "Old bug", "priority": 1,
         "createdAt": "2026-08-01T00:00:00.000Z", "labels": {"nodes": [{"name": "Bug"}]}}]


def test_build_project_boards_builds_five_panels_per_project():
    helio = _FakeHelio()
    board_ids = {"Helio": "dash-helio", "Concertino": "dash-concertino"}

    with patch("news.projects.build.linear.fetch_completed", return_value=_COMPLETED), \
         patch("news.projects.build.linear.fetch_open", return_value=_OPEN), \
         patch("news.projects.build.gitlog.fetch_recent_subjects", return_value=["HEL-1 Ship the thing"]), \
         patch("news.projects.build.narrative.project_summary_pass", return_value="Shipped one thing."):
        asyncio.run(build.build_project_boards(_config(), helio, board_ids))

    # 2 CSV sources (completed + open) per project x 2 projects
    assert len(helio.csv_sources_created) == 4
    # velocity (shape) + avg cycle time (shape) + oldest open (shape) = 3 shape pipelines per project
    assert len(helio.shape_pipelines_built) == 6
    # open bug count = 1 steps pipeline per project
    assert len(helio.steps_pipelines_built) == 2
    # 4 pipeline-bound panels (velocity, avg cycle time, open bugs, oldest open)
    # + 1 markdown narrative panel = 5 per project, x2 projects
    assert len(helio.panels_bound) == 8
    assert len(helio.text_panels_added) == 2

    # Source names must match the news-proj-<slug>-src-<key> pattern
    # cleanup_news_resources() sweeps on — a naming typo here would silently
    # orphan resources forever with no error.
    assert [n for n, _ in helio.csv_sources_created][:2] == [
        "news-proj-helio-src-completed", "news-proj-helio-src-open"]
    assert [n for n, _ in helio.csv_sources_created][2:] == [
        "news-proj-concertino-src-completed", "news-proj-concertino-src-open"]

    # The prefix reaching build_shape_pipeline (and, by construction, the
    # pipeline/output-DataType names built from it) must carry the same slug.
    helio_prefixes = {p for _, p, *_ in helio.shape_pipelines_built[:3]}
    assert helio_prefixes == {"news-proj-helio"}
    concertino_prefixes = {p for _, p, *_ in helio.shape_pipelines_built[3:]}
    assert concertino_prefixes == {"news-proj-concertino"}

    # Each project gets exactly one set_layout call positioning all 5 of its
    # own panels (no undefined x/y/w/h in production). The layout's panelIds
    # must be exactly the ids bind_new_panel/add_text_panel actually returned
    # for that project — not some other project's, not fewer than all 5.
    assert len(helio.layouts_set) == 2

    def _expected_panel_ids(dashboard_id):
        bound = {t[-1] for t in helio.panels_bound if t[0] == dashboard_id}
        text = {t[-1] for t in helio.text_panels_added if t[0] == dashboard_id}
        return bound | text

    for dashboard_id, items in helio.layouts_set:
        assert len(items) == 5
        layout_panel_ids = {item["panelId"] for item in items}
        assert layout_panel_ids == _expected_panel_ids(dashboard_id)
        # every item carries real grid coordinates, not None placeholders
        for item in items:
            assert all(isinstance(item[k], int) for k in ("x", "y", "w", "h"))


def test_build_project_boards_skips_disabled():
    helio = _FakeHelio()
    config = _config()
    config["projects"]["enabled"] = False

    asyncio.run(build.build_project_boards(config, helio, {"Helio": "dash-helio"}))

    assert helio.csv_sources_created == []


def test_build_project_boards_skips_one_project_on_fetch_failure_not_the_other():
    helio = _FakeHelio()
    board_ids = {"Helio": "dash-helio", "Concertino": "dash-concertino"}

    def fetch_completed(team, lookback):
        if team == "Helio Platform":
            raise RuntimeError("Linear API error: boom")
        return _COMPLETED

    with patch("news.projects.build.linear.fetch_completed", side_effect=fetch_completed), \
         patch("news.projects.build.linear.fetch_open", return_value=_OPEN), \
         patch("news.projects.build.gitlog.fetch_recent_subjects", return_value=[]), \
         patch("news.projects.build.narrative.project_summary_pass", return_value=""):
        asyncio.run(build.build_project_boards(_config(), helio, board_ids))

    # Concertino (the second project) still built its 5 panels despite Helio's failure
    assert len(helio.text_panels_added) == 1
    assert helio.text_panels_added[0][0] == "dash-concertino"
    # ...and got its own layout call; Helio never reached set_layout at all
    assert len(helio.layouts_set) == 1
    assert helio.layouts_set[0][0] == "dash-concertino"
