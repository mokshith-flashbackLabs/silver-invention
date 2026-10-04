"""Weight and tag suggestions (spec §4.6), the pure half: which evidence the model sees, what is
kept of its answer, and the per-option read. No database."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from imageshield.intel import approvable
from imageshield.intel.models import Run
from imageshield.intel.proposal_models import ContextSignal, ProposalRecord, SuggestedTag
from imageshield.intel.recency import Recency
from imageshield.intel.schemas import ProposedTag, SuggestedOptionWeight, SuggestionOutput
from imageshield.intel.suggestion import (
    OptionSuggestion,
    SuggestionCandidates,
    mentions,
    render_suggestion,
    select_context,
    slugs_named_by,
    suggestion_options,
    validate_suggestion,
)
from tests.intel_fakes import scoring

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
