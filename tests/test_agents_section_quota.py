"""Per-section triage quota.

Triage used to rank the day's stories GLOBALLY and keep the top N. On a
tech-heavy day that returned 7 tech stories and 1 politics story, and the
Sports / Markets & Business boards rendered empty — starved, not broken.
`apply_section_quota` reserves each configured section board a slot budget
first, then fills what's left by importance.
"""

from news.agents import apply_section_quota

# board-name routing exactly as run._domain_to_board builds it
ROUTING = {
    "politics": "Politics & World", "world": "Politics & World",
    "tech": "Tech & AI", "ai": "Tech & AI",
    "sports": "Sports",
    "markets": "Markets & Business", "business": "Markets & Business",
}


def s(slug, domain, importance):
    return {"slug": slug, "domain": domain, "importance": importance}


def test_reserves_slots_for_a_section_that_loses_on_global_importance():
    stories = [
        s("t1", "tech", 5), s("t2", "ai", 5), s("t3", "tech", 4), s("t4", "tech", 4),
        s("p1", "politics", 3),
        s("sp1", "sports", 2), s("sp2", "sports", 1),
        s("m1", "markets", 2),
    ]
    kept = apply_section_quota(stories, ROUTING, per_section=2, top=8)

    boards = {ROUTING[x["domain"]] for x in kept}
    assert boards == {"Tech & AI", "Politics & World", "Sports", "Markets & Business"}
    assert {x["slug"] for x in kept} >= {"sp1", "m1"}


def test_caps_a_dominant_section_at_its_quota_before_the_fill_round():
    stories = [s(f"t{i}", "tech", 5) for i in range(6)] + [s("sp1", "sports", 1)]
    kept = apply_section_quota(stories, ROUTING, per_section=2, top=4)

    slugs = {x["slug"] for x in kept}
    assert "sp1" in slugs                      # reserved despite importance 1
    assert len([x for x in kept if x["domain"] == "tech"]) == 3   # 2 quota + 1 fill
    assert len(kept) == 4


def test_never_exceeds_the_overall_top():
    stories = [s(f"x{i}", d, 3) for i, d in enumerate(
        ["tech", "ai", "sports", "markets", "politics", "world", "business"])]
    assert len(apply_section_quota(stories, ROUTING, per_section=3, top=4)) == 4


def test_unused_quota_is_given_back_to_the_fill_round():
    """Nothing sports-y today: those slots go to the strongest leftovers, not waste."""
    stories = [s("t1", "tech", 5), s("t2", "tech", 4), s("t3", "tech", 3), s("p1", "politics", 2)]
    kept = apply_section_quota(stories, ROUTING, per_section=2, top=4)
    assert len(kept) == 4


def test_output_stays_ordered_by_importance():
    stories = [s("a", "tech", 2), s("b", "sports", 5), s("c", "politics", 4)]
    kept = apply_section_quota(stories, ROUTING, per_section=1, top=3)
    assert [x["slug"] for x in kept] == ["b", "c", "a"]


def test_domains_are_normalized_before_routing():
    """Triage emits free text — ' Sports' and 'Tech' must not vanish."""
    stories = [s("a", " Sports ", 3), s("b", "TECH", 4)]
    kept = apply_section_quota(stories, ROUTING, per_section=1, top=2)
    assert {x["slug"] for x in kept} == {"a", "b"}


def test_unroutable_domain_can_still_be_kept_by_the_fill_round():
    """`general` maps to no board — it belongs in the overview digest, so it
    competes for fill slots rather than being silently dropped at triage."""
    stories = [s("g1", "general", 5), s("t1", "tech", 1)]
    kept = apply_section_quota(stories, ROUTING, per_section=1, top=2)
    assert {x["slug"] for x in kept} == {"g1", "t1"}


def test_empty_input_is_safe():
    assert apply_section_quota([], ROUTING, per_section=2, top=8) == []
