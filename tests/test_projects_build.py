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
        self.panels_bound.append((dashboard_id, title, panel_type, output_type_id, mapping, chart_type))
        return f"panel-{len(self.panels_bound)}"

    async def add_text_panel(self, dashboard_id, title, content, markdown=True):
        self.text_panels_added.append((dashboard_id, title, content))
        return f"panel-text-{len(self.text_panels_added)}"


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
