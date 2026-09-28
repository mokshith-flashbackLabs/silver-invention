"""Pure helpers behind INVARIANTS #49 (verbatim quotes) and #50 (publishers)."""

from __future__ import annotations

from imageshield.intel.pii import MASK, contains_pii, mask
from imageshield.intel.publisher import publisher_domain
from imageshield.intel.tags import TagRegistry, is_well_formed, membership_problems
from imageshield.intel.text import content_sha256, normalise
from imageshield.intel.verify import VerifiedQuote, verify_quote

DOC = normalise(
    "Instagram  updated its\u00a0privacy policy on Monday.\n\n"
    "The new terms allow public profile photos to be used for AI training by default."
)


def test_normalise_collapses_whitespace_and_nbsp() -> None:
    assert normalise("a \u00a0 b\n\tc  ") == "a b c"


def test_hash_is_of_the_normalised_text() -> None:
    assert content_sha256(normalise("a  b")) == content_sha256(normalise("a b"))


def test_a_verbatim_quote_verifies_with_offsets() -> None:
    quote = "public profile photos to be used for AI training by default"
    result = verify_quote(DOC, quote)
    assert isinstance(result, VerifiedQuote)
    assert DOC[result.char_start : result.char_end] == quote


def test_whitespace_differences_in_the_quote_still_verify() -> None:
    result = verify_quote(DOC, "Instagram updated its   privacy policy on Monday.")
    assert isinstance(result, VerifiedQuote)


def test_a_paraphrase_is_rejected() -> None:
    assert verify_quote(DOC, "Instagram now trains AI on public photos by default") == (
        "not_a_substring"
    )


def test_length_bounds() -> None:
    assert verify_quote(DOC, "Instagram") == "too_short"
    assert verify_quote("a" * 700, "a" * 601) == "too_long"


def test_a_quote_holding_a_phone_or_email_is_dropped_not_redacted() -> None:
    doc = normalise("Call the support line on +1 415 555 0134 or write to help@example.com today.")
    assert verify_quote(doc, "Call the support line on +1 415 555 0134 or") == "pii_in_excerpt"
    assert verify_quote(doc, "or write to help@example.com today.") == "pii_in_excerpt"


def test_mask_counts_and_replaces_phone_and_email() -> None:
    masked, count = mask("ring +44 20 7946 0958 or mail a.b@c.org")
    assert MASK in masked and "7946" not in masked and "a.b@c.org" not in masked
    assert count == 2
    assert contains_pii("mail a.b@c.org")
    assert not contains_pii("posted on 2026-09-27 under id 3f2b0c1e-9d7a-4c2e-8f1a-0b9e6c5d4a31")


def test_publisher_is_the_registrable_domain_of_the_final_url() -> None:
    assert publisher_domain("https://news.example.com/a") == "example.com"
    assert publisher_domain("https://www.example.com/b") == "example.com"
    assert publisher_domain("https://www.bbc.co.uk/news/x") == "bbc.co.uk"


def test_private_suffixes_collapse_to_one_publisher() -> None:
    # ICANN section only: alice.github.io and bob.github.io are ONE publisher (#50 errs strict).
    assert publisher_domain("https://alice.github.io/p") == publisher_domain(
        "https://bob.github.io/q"
    )


def test_ip_literals_fall_back_to_the_host() -> None:
    assert publisher_domain("https://93.184.216.34/x") == "93.184.216.34"


def test_slug_shape_accepts_x_and_refuses_malformed() -> None:
    assert is_well_formed("x") and is_well_formed("dating_apps") and is_well_formed("a" * 40)
    assert not is_well_formed("") and not is_well_formed("X") and not is_well_formed("1abc")
    assert not is_well_formed("a" * 41) and not is_well_formed("insta-gram")


def test_membership_checks_only_what_is_added() -> None:
    registry = TagRegistry(active=frozenset({"instagram", "x"}), retired=frozenset({"vine"}))
    unknown, retired = membership_problems(["instagram", "vine", "bumble"], registry)
    assert unknown == ["bumble"] and retired == ["vine"]
