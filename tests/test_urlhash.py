"""Normalisation v1 — the dedup key. url_hash = sha256(canonical_url).

These tests pin the v1 rules permanently: changing any of them invalidates
every stored hash, so a failure here means you are shipping v2, not fixing a
bug (see NORMALISATION_VERSION and the version column on content_urls)."""

from __future__ import annotations

import hashlib

from imageshield.search.urlhash import (
    NORMALISATION_VERSION,
    canonicalise,
    source_domain,
    url_hash,
)


def test_version_is_v1() -> None:
    assert NORMALISATION_VERSION == "v1"


def test_hash_is_sha256_of_canonical_url() -> None:
    url = "https://Example.com/a?b=1#frag"
    assert url_hash(url) == hashlib.sha256(canonicalise(url).encode()).hexdigest()


def test_five_tracking_variants_produce_one_hash() -> None:
    base = "https://example.com/gallery/image?id=42"
    variants = [
        "https://example.com/gallery/image?id=42&utm_source=x",
        "https://example.com/gallery/image?utm_medium=y&id=42&utm_campaign=z",
        "https://example.com/gallery/image?fbclid=abc123&id=42",
        "https://example.com/gallery/image?id=42&gclid=g1&igshid=i1",
        "https://example.com/gallery/image?_ga=1.2&id=42&ref=home&yclid=9",
    ]
    hashes = {url_hash(v) for v in variants}
    assert hashes == {url_hash(base)}


def test_spec_example_canonicalises_but_scheme_is_preserved() -> None:
    # The two URLs from the step-6 spec. Scheme differs -> NOT equal.
    messy = "http://Example.COM:80/a/./b/../c/?b=2&utm_source=x&a=1#frag"
    assert canonicalise(messy) == "http://example.com/a/c?a=1&b=2"
    https_twin = "https://example.com/a/c?a=1&b=2"
    assert canonicalise(https_twin) == https_twin
    assert url_hash(messy) != url_hash(https_twin)  # http vs https is a real difference
    assert url_hash(messy) == url_hash("http://example.com/a/c?a=1&b=2")


def test_host_is_lowercased_and_punycoded_path_case_preserved() -> None:
    assert canonicalise("https://ExÄmple.com/Photo.JPG") == (
        "https://xn--exmple-cua.com/Photo.JPG"
    )
    # paths are case-sensitive: these must NOT collapse
    assert url_hash("https://example.com/A") != url_hash("https://example.com/a")


def test_default_ports_stripped_others_kept() -> None:
    assert canonicalise("http://example.com:80/x") == "http://example.com/x"
    assert canonicalise("https://example.com:443/x") == "https://example.com/x"
    assert canonicalise("https://example.com:8443/x") == "https://example.com:8443/x"


def test_fragment_stripped_and_query_sorted() -> None:
    assert canonicalise("https://example.com/p?b=2&a=1#top") == "https://example.com/p?a=1&b=2"


def test_percent_encoding_unreserved_decoded_reserved_uppercased() -> None:
    # %7e is '~' (unreserved) -> decoded; %2f is '/' (reserved) -> kept, hex uppercased
    assert canonicalise("https://example.com/%7euser/a%2fb") == (
        "https://example.com/~user/a%2Fb"
    )


def test_trailing_slash_stripped_except_bare_root() -> None:
    assert canonicalise("https://example.com/a/") == "https://example.com/a"
    assert canonicalise("https://example.com/") == "https://example.com/"
    assert canonicalise("https://example.com") == "https://example.com/"
    assert url_hash("https://example.com") == url_hash("https://example.com/")


def test_garbage_is_canonicalised_verbatim_never_raises() -> None:
    assert canonicalise("not a url") == "not a url"
    assert canonicalise("http://bad:port:99999/") == "http://bad:port:99999/"
    assert len(url_hash("not a url")) == 64


def test_source_domain_uses_canonical_host() -> None:
    assert source_domain("https://Sub.Example.com:443/x/y.jpg") == "sub.example.com"
    assert source_domain("not a url") == "unknown"
