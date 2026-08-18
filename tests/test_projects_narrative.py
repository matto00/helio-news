from unittest.mock import MagicMock

from news.projects import narrative


def test_project_summary_pass_returns_summary_from_ollama_response():
    ollama = MagicMock()
    ollama.chat_json.return_value = {"summary": "Shipped the pipeline detail redesign and MFA."}

    result = narrative.project_summary_pass(
        ollama, "gpt-oss:latest", "Helio",
        completed_titles=["Redesign pipeline detail page", "Add TOTP-based MFA"],
        commit_subjects=["HEL-719 Redesign the pipeline detail page chrome",
                         "HEL-702 Add TOTP-based MFA"],
        think="medium")

    assert result == "Shipped the pipeline detail redesign and MFA."
    ollama.chat_json.assert_called_once()
    call = ollama.chat_json.call_args
    assert call.args[0] == "gpt-oss:latest"  # model
    assert "Redesign pipeline detail page" in call.args[2]  # user payload
    assert call.kwargs.get("think") == "medium"


def test_project_summary_pass_returns_empty_string_on_empty_response():
    ollama = MagicMock()
    ollama.chat_json.return_value = {}

    result = narrative.project_summary_pass(
        ollama, "gpt-oss:latest", "Helio", completed_titles=[], commit_subjects=[])

    assert result == ""


def test_project_summary_pass_returns_empty_string_on_non_dict_response():
    ollama = MagicMock()
    ollama.chat_json.return_value = ["unexpected", "list", "response"]

    result = narrative.project_summary_pass(
        ollama, "gpt-oss:latest", "Helio", completed_titles=[], commit_subjects=[])

    assert result == ""
