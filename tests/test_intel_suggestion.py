"""Weight and tag suggestions (spec §4.6), the pure half: which evidence the model sees, what is
kept of its answer, and the per-option read. No database."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from imageshield.intel import approvable, prompts
from imageshield.intel.models import Run
from imageshield.intel.proposal_models import ContextSignal, ProposalRecord, SuggestedTag
from imageshield.intel.recency import Recency
from imageshield.intel.schemas import ProposedTag, SuggestedOptionWeight, SuggestionOutput
from imageshield.intel.suggestion import (
    OptionResearch,
    OptionSuggestion,
    ResearchSearch,
    SuggestionCandidates,
    attach_research,
    is_backed,
    keep_backed,
    mentions,
    plan_research,
    render_suggestion,
    select_context,
    slugs_named_by,
    suggestion_options,
    validate_suggestion,
)
from tests.intel_fakes import scoring
from tests.question_fakes import QUESTION

# The read flags at a fixed moment: none of these proposals is a threat, so the window is moot.
read_flags = partial(approvable.read_flags, recency=Recency(datetime.now(UTC), 90))

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _signal(
    *,
    tags: tuple[str, ...] = (),
    subjects: tuple[str, ...] = (),
    summary: str = "s",
    category: str = "policy",
    trust: str = "listed",
    publisher: str = "p.example",
    status: str = "active",
) -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category=category,
        direction="risk_up",
        tags=tags,
        unregistered_subjects=subjects,
        summary=summary,
        trust=trust,  # type: ignore[arg-type]
        publisher_domain=publisher,
        status=status,
        created_at=T0,
    )


def test_mentions_is_exact_on_words_never_fuzzy() -> None:
    assert mentions("Bumble", "Bumble") and mentions("bumble's new policy", "Bumble")
    assert mentions("Twitter changed its terms", "X (Twitter)")  # a word of four letters or more
    assert not mentions("Bumbl", "Bumble")
    assert not mentions("No change to the policy", "No")  # a short option matches only outright
    assert mentions("x", "X") and not mentions("x marks the spot", "X")


def test_a_registered_tag_the_option_names_is_retrieved_by_tag() -> None:
    """spec §4.6 "by subject": "Bumble" finds `bumble` evidence before anybody maps it."""
    assert slugs_named_by(["LinkedIn", "Bumble"], scoring()) == frozenset({"linkedin"})
    assert slugs_named_by(["Bumble"], None) == frozenset()


def test_retrieval_takes_chosen_sources_first_then_tag_subject_and_category_bounded() -> None:
    from_source = _signal(summary="read from a chosen source")
    tagged = _signal(tags=("instagram",))
    subject = _signal(subjects=("Bumble",))
    other_subject = _signal(subjects=("Hinge",))
    research = _signal(category="research", summary="A Bumble study of photo reuse")
    tooling = _signal(category="tooling", summary="Bumble tooling")
    retracted = _signal(tags=("instagram",), status="retracted")
    candidates = SuggestionCandidates(
        from_sources=(from_source,),
        by_tag=(tagged, from_source, retracted),
        with_subjects=(subject, other_subject),
        by_category=(research, tooling),
    )
    options = ["Instagram", "Bumble"]
    assert select_context(candidates, options=options, limit=80) == [
        from_source,
        tagged,
        subject,
        research,
    ]
    assert select_context(candidates, options=options, limit=2) == [from_source, tagged]


def test_a_malformed_option_loses_only_its_bad_values() -> None:
    """Review Focus 5: withheld and counted, never clamped or invented; the rest stands."""
    v = scoring()  # instagram and linkedin active, myspace retired
    good = _signal(tags=("instagram",))
    web = _signal(trust="web", publisher="a.example")
    output = SuggestionOutput(
        options=[
            SuggestedOptionWeight(
                option="Instagram",
                deduction=4,
                rationale="Public by default; mail press@example.com",
                signal_ids=[str(good.signal_id)],
                suggested_tags=["instagram"],
            ),
            SuggestedOptionWeight(
                option="Bumble",
                deduction=14,
                rationale="r",
                signal_ids=[str(web.signal_id), str(uuid4()), "not-a-uuid"],
                suggested_tags=["myspace", "madeup"],
                new_tag=ProposedTag(slug="linkedin", label="LinkedIn", kind="platform"),
            ),
            SuggestedOptionWeight(option="Tinder", deduction=3, rationale="not asked"),
        ]
    )
    counts: Counter[str] = Counter()
    instagram, bumble, hinge = validate_suggestion(
        output,
        options=["Instagram", "Bumble", "Hinge"],
        cap=8,
        context={good.signal_id: good, web.signal_id: web},
        vocabulary=v,
        counts=counts,
    )
    assert instagram == OptionSuggestion(
        "Instagram", 4, "Public by default; mail «masked»", (good.signal_id,), ("instagram",), None
    )
    assert bumble == OptionSuggestion("Bumble", None, "r", (web.signal_id,), (), None)
    assert hinge == OptionSuggestion("Hinge", None, "", (), (), None)
    assert counts == Counter(
        {
            "pii_masked_suggestion_rationale": 1,
            "suggestion_deduction_out_of_bounds": 1,
            "suggestion_signal_dropped_unknown": 2,
            "suggestion_tag_dropped_retired": 1,
            "suggestion_tag_dropped_unknown": 1,
            "suggestion_new_tag_dropped_invalid": 1,
            "suggestion_option_unknown": 1,
            "suggestion_option_missing": 1,
        }
    )


def test_a_deduction_needs_evidence_and_respects_the_cap() -> None:
    good = _signal(tags=("instagram",))
    cited = [str(good.signal_id)]
    output = SuggestionOutput(
        options=[
            SuggestedOptionWeight(option="A", deduction=5),  # no evidence: never a number
            SuggestedOptionWeight(option="B", deduction=9, signal_ids=cited),  # above the cap
            SuggestedOptionWeight(option="C", deduction=0, signal_ids=cited),  # zero is a number
        ]
    )
    counts: Counter[str] = Counter()
    a, b, c = validate_suggestion(
        output,
        options=["A", "B", "C"],
        cap=8,
        context={good.signal_id: good},
        vocabulary=scoring(),
        counts=counts,
    )
    assert (a.deduction, b.deduction, c.deduction) == (None, None, 0)
    assert counts["suggestion_deduction_without_evidence"] == 1
    assert counts["suggestion_deduction_out_of_bounds"] == 1


def test_a_new_tag_is_offered_only_when_no_registered_tag_fits() -> None:
    output = SuggestionOutput(
        options=[
            SuggestedOptionWeight(
                option="Bumble", new_tag=ProposedTag(slug="bumble", label="Bumble", kind="platform")
            ),
            SuggestedOptionWeight(
                option="Instagram",
                suggested_tags=["instagram"],
                new_tag=ProposedTag(slug="insta", label="Insta", kind="platform"),
            ),
            SuggestedOptionWeight(
                option="Hinge", new_tag=ProposedTag(slug="Hinge!", label="Hinge", kind="platform")
            ),
        ]
    )
    counts: Counter[str] = Counter()
    bumble, instagram, hinge = validate_suggestion(
        output,
        options=["Bumble", "Instagram", "Hinge"],
        cap=None,
        context={},
        vocabulary=scoring(),
        counts=counts,
    )
    assert bumble.new_tag == SuggestedTag(slug="bumble", label="Bumble", kind="platform")
    assert instagram.new_tag is None and hinge.new_tag is None
    assert counts["suggestion_new_tag_dropped_a_tag_fits"] == 1
    assert counts["suggestion_new_tag_dropped_invalid"] == 1


def test_the_read_side_judges_each_option_by_its_own_signals() -> None:
    listed = _signal()
    web_one = _signal(trust="web", publisher="a.example")
    web_same = _signal(trust="web", publisher="a.example")
    web_two = _signal(trust="web", publisher="b.example")
    gone = _signal(status="retracted")

    def option(name: str, *signals: ContextSignal, deduction: int | None = 3) -> dict[str, object]:
        return {
            "option": name,
            "deduction": deduction,
            "rationale": "r",
            "signal_ids": [str(s.signal_id) for s in signals],
            "suggested_tags": [],
            "new_tag": None,
        }

    target = {
        "question_key": "platforms",
        "options": [
            option("A", listed),
            option("B", web_one, web_same),  # two pages of ONE publisher
            option("C", web_one, web_two),
            option("D", gone, deduction=None),
            option("E", deduction=None),
        ],
    }
    rows = suggestion_options(target, [listed, web_one, web_same, web_two, gone])
    assert [(r["option"], r["corroborated"], r["why_not"]) for r in rows] == [
        ("A", True, None),
        ("B", False, "uncorroborated"),
        ("C", True, None),
        ("D", False, "evidence_retracted"),
        ("E", False, "no_evidence"),
    ]
    assert rows[0]["deduction"] == 3 and rows[0]["signal_ids"] == [str(listed.signal_id)]


def _suggestion_run(status: str, outcome: dict[str, object]) -> Run:
    return Run(
        run_id=uuid4(),
        kind="weight_suggestion",
        source_id=None,
        request={},
        status=status,
        attempts=1,
        requested_by="ann",
        outcome=outcome,
        error_code=None,
        created_at=T0,
        completed_at=T0 if status == "completed" else None,
    )


def test_the_poll_has_no_options_until_a_suggestion_is_written() -> None:
    queued = _suggestion_run("queued", {})
    assert render_suggestion(queued, None) == {
        "run_id": queued.run_id,
        "status": "queued",
        "proposal_id": None,
        "error_code": None,
        "options": None,
        "sources_deferred": 0,
        "research_round": 0,
        "awaiting_sources": 0,
        "wait_deadline": None,
    }
    done = _suggestion_run("completed", {"sources_deferred": 2, "model_calls": 5})
    proposal_id = uuid4()
    options = [{"option": "Instagram", "corroborated": True, "why_not": None}]
    body = render_suggestion(done, (proposal_id, options))
    assert (body["proposal_id"], body["options"], body["sources_deferred"]) == (
        proposal_id,
        options,
        2,
    )


def test_a_suggestion_that_cites_nothing_is_not_evidence_retracted() -> None:
    """Review Focus 5: "no evidence, operator's call" is an answer, not a retraction."""
    suggestion = ProposalRecord(
        proposal_id=uuid4(),
        kind="weight_suggestion",
        status="delivered",
        target={"question_key": "platforms", "options": []},
        suggested={},
        decided=None,
        against_release_no=2,
        created_at=T0,
    )
    flags = read_flags(suggestion, [], scoring())
    assert (flags["evidence_retracted"], flags["why_not"], flags["approvable"]) == (
        False,
        "not_decidable",
        False,
    )
    assert read_flags(suggestion, [_signal(status="retracted")], scoring())["evidence_retracted"]
    change = ProposalRecord(
        proposal_id=uuid4(),
        kind="weight_change",
        status="pending",
        target={"question_key": "platforms", "option": "Instagram", "current": 3},
        suggested={"delta": 1},
        decided=None,
        against_release_no=2,
        created_at=T0,
    )
    assert read_flags(change, [], scoring())["evidence_retracted"] is True  # unchanged


# ── research per answer (spec 2026-10-10-intel-every-answer-researched §2.2, §2.3) ──


def _answer(
    name: str, deduction: int | None, ids: tuple = (), searches: tuple[str, ...] = ()
) -> OptionSuggestion:
    return OptionSuggestion(
        name, deduction, "r", tuple(ids), (), None, research_searches=tuple(searches)
    )


def test_an_answer_is_backed_only_with_a_number_and_corroborated_cited_evidence() -> None:
    listed = _signal(trust="listed")
    web_a = _signal(trust="web", publisher="a.example")
    web_b = _signal(trust="web", publisher="b.example")
    retracted = _signal(trust="listed", status="retracted")
    context = {s.signal_id: s for s in (listed, web_a, web_b, retracted)}
    assert is_backed(_answer("A", 4, (listed.signal_id,)), context)
    assert is_backed(_answer("A", 4, (web_a.signal_id, web_b.signal_id)), context)
    assert not is_backed(_answer("A", None, (listed.signal_id,)), context)
    assert not is_backed(_answer("A", 4), context)
    assert not is_backed(_answer("A", 4, (web_a.signal_id,)), context)  # one web publisher
    assert not is_backed(_answer("A", 4, (retracted.signal_id,)), context)
    assert not is_backed(_answer("A", 4, (uuid4(),)), context)  # not in what the model saw


def test_plan_research_drops_people_repeats_and_overflow() -> None:
    counts: Counter[str] = Counter()
    planned = plan_research(
        [
            _answer("A", None, searches=("first query", "call +44 20 7946 0958", "FIRST  query")),
            _answer("B", None, searches=("one", "two", "three")),
            _answer("C", None),
        ],
        searched={"B": [{"round": 1, "query": "one"}]},
        per_answer=1,
        counts=counts,
    )
    assert planned == {"A": ["first query"], "B": ["two"]}
    assert counts["research_search_names_a_person"] == 1
    assert counts["research_search_repeated"] == 2
    assert counts["research_search_over_cap"] == 1


def test_attach_research_counts_rounds_and_marks_unbacked_answers_exhausted() -> None:
    searched = {"A": [{"round": 1, "query": "q1"}, {"round": 2, "query": "q2"}]}
    a, b = attach_research(
        [_answer("A", None), _answer("B", 3)],
        backed={"A": False, "B": True},
        searched=searched,
        researching=True,
    )
    assert a.research == OptionResearch(
        rounds=2, searches=(ResearchSearch(1, "q1"), ResearchSearch(2, "q2")), exhausted=True
    )
    assert b.research == OptionResearch(rounds=0, searches=(), exhausted=False)
    assert a.as_json()["research"] == {
        "rounds": 2,
        "searches": [{"round": 1, "query": "q1"}, {"round": 2, "query": "q2"}],
        "exhausted": True,
    }


def test_an_answer_never_searched_for_is_not_exhausted() -> None:
    """Final review I2 (2026-10-11): nothing found means nothing found after a search ran."""
    (only,) = attach_research(
        [_answer("A", None)], backed={"A": False}, searched={}, researching=True
    )
    assert only.research == OptionResearch(rounds=0, searches=(), exhausted=False)


def test_with_research_off_nothing_is_exhausted() -> None:
    (only,) = attach_research(
        [_answer("A", None)], backed={"A": False}, searched={}, researching=False
    )
    assert only.research == OptionResearch(rounds=0, searches=(), exhausted=False)


def test_an_option_round_trips_through_its_json_without_its_asked_searches() -> None:
    original = OptionSuggestion(
        "A", 4, "r", (uuid4(),), ("instagram",), SuggestedTag(slug="x", label="X", kind="platform")
    )
    assert OptionSuggestion.from_json(original.as_json()) == original
    assert "research" not in original.as_json()  # no research record until attached
    asked = _answer("A", None, searches=("q",))
    assert "research_searches" not in asked.as_json()  # the model's ask is never stored


def test_validate_suggestion_carries_the_models_research_searches() -> None:
    output = SuggestionOutput(
        options=[SuggestedOptionWeight(option="Bumble", research_searches=["q1", "q2"])]
    )
    (bumble,) = validate_suggestion(
        output, options=["Bumble"], cap=None, context={}, vocabulary=scoring(), counts=Counter()
    )
    assert bumble.research_searches == ("q1", "q2")


def test_suggestion_options_passes_the_research_record_through() -> None:
    target = {
        "question_key": "q",
        "options": [
            {
                "option": "A",
                "deduction": None,
                "signal_ids": [],
                "research": {"rounds": 1, "searches": [], "exhausted": True},
            }
        ],
    }
    (row,) = suggestion_options(target, [])
    assert row["research"] == {"rounds": 1, "searches": [], "exhausted": True}


def test_no_fixture_answer_or_example_appears_in_the_fixed_prompt_text() -> None:
    """No steering (owner, 2026-10-09): the rules are general."""
    fixed = prompts._SOURCE_PROPOSAL_SYSTEM + prompts._SUGGEST_SYSTEM
    for answer in QUESTION["options"]:
        assert answer not in fixed
    assert "by gender" not in fixed and "by age" not in fixed
    assert prompts.SUGGEST_PROMPT_VERSION == "suggest-v2"


def _question() -> prompts.PromptSuggestionQuestion:
    return {"key": "k", "prompt": "p", "type": None, "cap": None, "options": []}


def test_the_payload_asks_for_research_only_while_rounds_remain() -> None:
    def research(round_no: int, max_rounds: int) -> object:
        _, user = prompts.suggestion_request(
            _question(),
            [],
            registry_tags=[],
            research_round=round_no,
            max_rounds=max_rounds,
            searches_per_answer=3,
        )
        return json.loads(user)["research"]

    assert research(1, 4) == {"round": 1, "max_rounds": 4, "searches_per_answer": 3}
    assert research(4, 4) == {"round": 4, "max_rounds": 4, "searches_per_answer": 3}
    assert research(5, 4) is None  # the last pass: no more searches to ask for
    assert research(1, 0) is None  # research off


# ── final review fixes (2026-10-11) ──


def test_research_evidence_is_taken_before_the_other_classes() -> None:
    """C1: research evidence has its own class, taken first, so it reaches the model."""
    research = [_signal(tags=("r",)) for _ in range(3)]
    chosen = [_signal(tags=("c",)) for _ in range(3)]
    context = select_context(
        SuggestionCandidates(from_sources=tuple(chosen), from_research=tuple(research)),
        options=["A"],
        limit=4,
    )
    assert [s.signal_id for s in context] == [s.signal_id for s in research] + [chosen[0].signal_id]


def test_keep_backed_keeps_an_answer_an_earlier_pass_backed() -> None:
    """C1: a later pass never loses an answer an earlier pass backed."""
    listed = _signal(trust="listed")
    earlier = [_answer("A", 4, (listed.signal_id,)), _answer("B", None)]
    later = [_answer("A", None), _answer("B", 2, (listed.signal_id,))]
    counts: Counter[str] = Counter()
    merged, backed = keep_backed(
        later,
        backed={"A": False, "B": True},
        kept=[o.as_json() for o in earlier],
        kept_backed={"A": True, "B": False},
        counts=counts,
    )
    assert [(o.option, o.deduction) for o in merged] == [("A", 4), ("B", 2)]
    assert backed == {"A": True, "B": True}
    assert counts["research_kept_backed_answer"] == 1


def test_keep_backed_without_a_kept_pass_changes_nothing() -> None:
    later = [_answer("A", None)]
    merged, backed = keep_backed(
        later, backed={"A": False}, kept=None, kept_backed={}, counts=Counter()
    )
    assert merged == later and backed == {"A": False}


def test_the_validation_search_states_its_budget() -> None:
    """I6: the validation search's count comes from config, not 'exactly one'."""
    system, user = prompts.validation_search_request("q", searches=3)
    assert json.loads(user) == {"query": "q", "max_searches": 3}
    assert "exactly one" not in system and "max_searches" in system


def test_the_suggest_prompt_asks_for_a_search_for_every_unbacked_answer() -> None:
    """I2: an unbacked answer gets at least one search; 'give none' is gone."""
    assert "Give none when no search could help" not in prompts._SUGGEST_SYSTEM
    assert "at least one" in prompts._SUGGEST_SYSTEM
