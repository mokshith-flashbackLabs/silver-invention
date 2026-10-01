"""Choosing sources for a question (spec §4.10), the pure half: identity, an option's tags, and
which proposed candidates survive. No database."""

from __future__ import annotations

import re
from collections import Counter

from imageshield.intel.schemas import ProposedOptionSources, ProposedSource, SourceProposalOutput
from imageshield.intel.source_choice import (
    BLOCKED_REASONS,
    FETCH_REASONS,
    NOT_VALIDATED_REASONS,
    NewSource,
    candidate_key,
    clean_candidates,
    identity,
    is_https,
    merge_by_identity,
    option_tags,
    url_verdict,
)
from tests.intel_fakes import scoring


def test_identity_is_the_canonical_url_or_the_normalised_query() -> None:
    assert identity("policy_page", "https://P.example/terms?utm_source=x", None) == identity(
        "news", "https://p.example/terms", None
    )
    assert identity("search_query", None, "  Instagram   Privacy ") == identity(
        "search_query", None, "instagram privacy"
    )
    # A validation result is keyed by kind as well: the text floor depends on it.
    url = "https://p.example/terms"
    assert candidate_key("policy_page", url, None) != candidate_key("news", url, None)


def test_the_requests_tags_replace_the_vocabularys_map_even_when_empty() -> None:
    v = scoring()  # maps Instagram to instagram
    assert option_tags("Instagram", question_key="platforms", request_tags=None, vocabulary=v) == (
        "instagram",
    )
    assert option_tags("Instagram", question_key="platforms", request_tags={}, vocabulary=v) == ()
    assert option_tags(
        "Instagram", question_key="platforms", request_tags={"Instagram": ("x",)}, vocabulary=v
    ) == ("x",)
    assert option_tags("Bumble", question_key="platforms", request_tags=None, vocabulary=None) == ()


def test_candidates_keep_https_canonical_deduplicated_and_person_free() -> None:
    existing_url = "https://p.example/already"
    out = SourceProposalOutput(
        options=[
            ProposedOptionSources(
                option="Instagram",
                candidates=[
                    ProposedSource(
                        kind="policy_page",
                        source_url="https://P.example/terms?utm_source=x",
                        reason="its terms",
                    ),
                    ProposedSource(
                        kind="news", source_url="https://p.example/terms", reason="again"
                    ),
                    ProposedSource(
                        kind="policy_page", source_url="http://p.example/p", reason="http"
                    ),
                    ProposedSource(
                        kind="search_query",
                        query_text="Instagram leak call +44 20 7946 0958",
                        reason="names a phone",
                    ),
                    ProposedSource(
                        kind="search_query", query_text="Instagram privacy news", reason="n"
                    ),
                    ProposedSource(
                        kind="search_query",
                        source_url="https://p.example/x",
                        query_text="q",
                        reason="both",
                    ),
                    ProposedSource(
                        kind="policy_page", source_url=existing_url, reason="registered"
                    ),
                ],
            ),
            ProposedOptionSources(
                option="Tinder",
                candidates=[ProposedSource(kind="policy_page", source_url="https://t.example/a")],
            ),
        ]
    )
    counts: Counter[str] = Counter()
    existing = {"Instagram": frozenset({identity("policy_page", existing_url, None)})}
    cleaned = clean_candidates(
        out, options=["Instagram", "Bumble"], existing=existing, counts=counts
    )
    assert [(c.kind, c.source_url, c.query_text) for c in cleaned["Instagram"]] == [
        ("policy_page", "https://p.example/terms", None),
        ("search_query", None, "Instagram privacy news"),
    ]
    assert cleaned["Bumble"] == []
    assert counts == Counter(
        {
            "candidate_dropped_duplicate": 2,
            "candidate_dropped_not_https": 1,
            "candidate_dropped_query_names_a_person": 1,
            "candidate_dropped_malformed": 1,
            "candidate_dropped_unknown_option": 1,
        }
    )


def test_a_reason_is_masked_and_bounded() -> None:
    out = SourceProposalOutput(
        options=[
            ProposedOptionSources(
                option="Instagram",
                candidates=[
                    ProposedSource(
                        kind="news",
                        source_url="https://n.example/a",
                        reason="Mail press@example.com " + "x" * 400,
                    )
                ],
            )
        ]
    )
    counts: Counter[str] = Counter()
    (candidate,) = clean_candidates(out, options=["Instagram"], existing={}, counts=counts)[
        "Instagram"
    ]
    assert "press@example.com" not in candidate.reason and len(candidate.reason) == 300
    assert counts["pii_masked_candidate_reason"] == 1


def test_every_blocked_reason_is_a_token_the_backend_accepts() -> None:
    """Pinned for the backend's client (image_backend c02d78c): it drops any reason that does not
    match this pattern."""
    pattern = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
    assert BLOCKED_REASONS and all(pattern.fullmatch(reason) for reason in BLOCKED_REASONS)
    assert set(FETCH_REASONS.values()) <= BLOCKED_REASONS


def test_the_text_floor_depends_on_the_kind_and_a_feed_needs_items() -> None:
    assert url_verdict("policy_page", "x" * 499, None) == "too_short"
    assert url_verdict("policy_page", "x" * 500, None) is None
    assert url_verdict("news", "x" * 199, None) == "too_short"
    assert url_verdict("search_result", "x" * 200, None) is None
    assert url_verdict("feed", "", [{"title": "t", "link": "https://n.example/1"}]) is None
    assert url_verdict("feed", "x" * 5000, []) == "no_items"
    assert url_verdict("feed", "x" * 5000, None) == "no_items"  # it did not parse as a feed


def test_every_not_validated_reason_is_a_token_the_backend_accepts() -> None:
    pattern = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
    assert {"unknown_run", "expired", "not_ready"} == NOT_VALIDATED_REASONS
    assert all(pattern.fullmatch(reason) for reason in NOT_VALIDATED_REASONS)


def _new(option: str, tags: tuple[str, ...], url: str = "https://p.example/terms") -> NewSource:
    return NewSource(
        kind="policy_page",
        source_url=url,
        query_text=None,
        tags=tags,
        check_every_hours=168,
        terms_note="public terms page",
        origin="operator",
        question_key="platforms",
        option=option,
    )


def test_a_source_chosen_for_two_options_is_one_source_with_both_options_tags() -> None:
    merged = merge_by_identity(
        [
            _new("Instagram", ("instagram",)),
            _new("Bumble", (), url="https://b.example/terms"),
            _new("Threads", ("threads", "instagram")),
        ]
    )
    assert [(m.option, m.source_url, m.tags) for m in merged] == [
        ("Instagram", "https://p.example/terms", ("instagram", "threads")),
        ("Bumble", "https://b.example/terms", ()),
    ]


def test_a_url_with_no_host_is_not_https() -> None:
    assert is_https("https://p.example/terms")
    for bad in ("https://", "https:///path", "  https://  ", "http://p.example", ""):
        assert not is_https(bad)
