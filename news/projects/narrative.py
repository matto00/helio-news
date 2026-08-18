"""One gpt-oss pass: turn a project's recently-completed ticket titles and
git commit subjects into a short "what shipped" paragraph. Structurally the
narrative counterpart to the metrics panels — those are code-computed ground
truth (see metrics.py); this is the one place a model's read on the period
appears, same division of labor as the news pipeline's summarizer pass vs.
its code-computed history/trend numbers."""

from __future__ import annotations

import json

_SYSTEM = (
    "You write a short, specific 'what shipped' summary for one software "
    "project's recent activity, for a developer checking in on their own "
    "project. You're given completed ticket titles and recent git commit "
    "subjects (which often restate ticket work in more technical terms). "
    "Write 2-4 sentences, prose, no bullet points, no headers. Name specific "
    "features/fixes, not vague summaries like 'various improvements'. If "
    "there's nothing to report, say so plainly in one short sentence — "
    "never pad or invent activity.\n"
    'Return ONLY JSON: {"summary": "..."}'
)


def project_summary_pass(ollama, model: str, project_name: str,
                         completed_titles: list[str], commit_subjects: list[str],
                         think: str | None = None) -> str:
    user = json.dumps({
        "project": project_name,
        "completed_tickets": completed_titles,
        "recent_commits": commit_subjects,
    })
    result = ollama.chat_json(model, _SYSTEM, user, think=think)
    return result.get("summary", "")
