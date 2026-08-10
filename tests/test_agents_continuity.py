from news import agents, history


class FakeOllama:
    """Stub matching Ollama.chat_json's signature — no network/ollama needed."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, model, system, user, temperature=0.2, think=None):
        self.calls.append({"model": model, "system": system, "user": user,
                           "temperature": temperature, "think": think})
        return self.responses.pop(0) if self.responses else {}


def _candidate_dicts():
    return [{"day": "2026-08-08", "headline": "Fed signals a rate cut", "importance": 3}]


def _story():
    return {"headline": "Fed cuts rates a quarter point", "domain": "markets",
            "importance": 4, "breaking": False, "slug": "fed-rate-cut"}


def _window(day="2026-08-08", importance=3):
    entry = history.HistoryEntry(
        slug="fed-signals", headline="Fed signals a rate cut", subject="",
        domain="markets", importance=importance, breaking=False,
        sentiment="neutral", summary="", article_count=2,
        entities=["Federal Reserve"], day=day)
    return [entry]


def _window_same_day(importances=(2, 5)):
    """Two stored entries on the SAME day, different importance — a day's
    triage occasionally splits one real event into two slugs (fix 2)."""
    return [
        history.HistoryEntry(
            slug=f"fed-{i}", headline="Fed signals a rate cut", subject="",
            domain="markets", importance=imp, breaking=False,
            sentiment="neutral", summary="", article_count=1,
            entities=["Federal Reserve"], day="2026-08-08")
        for i, imp in enumerate(importances)
    ]


def _ground():
    return {"days_running": 2, "first_seen": "2026-08-08", "expected_trend": "rising"}


# ── historian_pass ───────────────────────────────────────────────────────────

def test_historian_pass_short_circuits_with_no_candidates():
    ollama = FakeOllama([])
    out = agents.historian_pass(ollama, "gpt-oss:latest", _story(), [])
    assert out == {"is_continuation": False, "trend": "steady", "note": ""}
    assert ollama.calls == []


def test_historian_pass_parses_a_valid_response():
    ollama = FakeOllama([{"is_continuation": True, "trend": "rising",
                          "note": "Second day of coverage."}])
    out = agents.historian_pass(ollama, "gpt-oss:latest", _story(), _candidate_dicts())
    assert out == {"is_continuation": True, "trend": "rising",
                   "note": "Second day of coverage."}


def test_historian_pass_invalid_trend_defaults_to_steady():
    ollama = FakeOllama([{"is_continuation": True, "trend": "sideways", "note": "x"}])
    out = agents.historian_pass(ollama, "gpt-oss:latest", _story(), _candidate_dicts())
    assert out["trend"] == "steady"


# ── verify_continuation ──────────────────────────────────────────────────────

def test_verify_continuation_true_when_model_confirms():
    ollama = FakeOllama([{"confirmed": True}])
    result = agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": True, "trend": "rising", "note": "x"}, _ground())
    assert result is True


def test_verify_continuation_false_when_model_rejects():
    ollama = FakeOllama([{"confirmed": False}])
    result = agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": True, "trend": "rising", "note": "x"}, _ground())
    assert result is False


def test_verify_continuation_short_circuits_when_historian_said_no():
    ollama = FakeOllama([])
    result = agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": False, "trend": "steady", "note": ""}, _ground())
    assert result is False
    assert ollama.calls == []


def test_verify_continuation_includes_ground_truth_in_prompt():
    """Proves the wiring: ground-truth facts actually reach the model prompt,
    so a claimed timeframe that contradicts them can be caught (fix 1)."""
    ollama = FakeOllama([{"confirmed": True}])
    ground = {"days_running": 4, "first_seen": "2026-08-05", "expected_trend": "rising"}
    agents.verify_continuation(
        ollama, "gpt-oss:latest", _story(), _candidate_dicts(),
        {"is_continuation": True, "trend": "rising", "note": "Fourth day."}, ground)
    user = ollama.calls[-1]["user"]
    assert "4" in user
    assert "2026-08-05" in user


# ── continuity_facts orchestrator ────────────────────────────────────────────

def test_continuity_facts_none_when_no_matching_candidates():
    ollama = FakeOllama([])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(), [], [],
        match_threshold=0.35)
    assert result is None
    assert ollama.calls == []


def test_continuity_facts_none_when_historian_says_not_a_continuation():
    ollama = FakeOllama([{"is_continuation": False, "trend": "steady", "note": ""}])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(), match_threshold=0.2)
    assert result is None


def test_continuity_facts_none_when_trend_claim_mismatches_ground_truth():
    # today's importance (4) vs earliest candidate (3) → expected_trend "rising";
    # historian claims "falling" — should be rejected before the verifier ever runs.
    ollama = FakeOllama([{"is_continuation": True, "trend": "falling", "note": "x"}])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(importance=3), match_threshold=0.2)
    assert result is None
    assert len(ollama.calls) == 1   # only the historian call — verifier never ran


def test_continuity_facts_none_when_verifier_rejects():
    ollama = FakeOllama([
        {"is_continuation": True, "trend": "rising", "note": "Second day."},
        {"confirmed": False},
    ])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(importance=3), match_threshold=0.2)
    assert result is None


def test_continuity_facts_confirmed_returns_full_dict():
    ollama = FakeOllama([
        {"is_continuation": True, "trend": "rising", "note": "Second day of coverage."},
        {"confirmed": True},
    ])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window(importance=3), match_threshold=0.2)
    assert result == {
        "is_continuation": True,
        "days_running": 2,
        "first_seen": "2026-08-08",
        "trend": "rising",
        "note": "Second day of coverage.",
        "occurrences": [{"day": "2026-08-08", "headline": "Fed signals a rate cut",
                         "importance": 3}],
    }


def test_continuity_facts_dedupes_same_day_candidates_keeping_highest_importance():
    # Two stored entries both land on 2026-08-08 (importance 2 and 5) — a day's
    # triage occasionally splits one real event into two slugs. Only the
    # higher-importance one (5) should survive, so days_running (2) and
    # len(occurrences) (1) stay consistent by construction.
    ollama = FakeOllama([
        {"is_continuation": True, "trend": "falling", "note": "Cooling off."},
        {"confirmed": True},
    ])
    result = agents.continuity_facts(
        ollama, "gpt-oss:latest", "gpt-oss:latest", _story(),
        ["Federal Reserve"], _window_same_day(), match_threshold=0.2)
    assert result is not None
    assert len(result["occurrences"]) == 1
    assert result["occurrences"][0]["importance"] == 5
    assert result["days_running"] == 2


def test_story_offers_includes_history_timeline_at_three_occurrences():
    offers = agents.story_offers({}, [], {}, has_image=False, n_facts=0,
                                 history_occurrences=3)
    keys = {k for k, _ in offers}
    assert "history:timeline" in keys


def test_story_offers_omits_history_timeline_below_three():
    offers = agents.story_offers({}, [], {}, has_image=False, n_facts=0,
                                 history_occurrences=2)
    keys = {k for k, _ in offers}
    assert "history:timeline" not in keys


def test_story_offers_defaults_history_occurrences_to_zero():
    offers = agents.story_offers({}, [], {}, has_image=False, n_facts=0)
    keys = {k for k, _ in offers}
    assert "history:timeline" not in keys
