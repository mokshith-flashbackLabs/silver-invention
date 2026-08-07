"""The interim hash is sha256 of the RAW url — step 6 replaces the module
with real normalisation. These tests pin the placeholder's behaviour so the
step-6 swap shows up as a deliberate test change, not a silent one."""

from __future__ import annotations

import hashlib

from imageshield.search.urlhash import interim_url_hash, source_domain


def test_interim_hash_is_sha256_of_raw_url() -> None:
    url = "https://Example.com/a?b=1"
    assert interim_url_hash(url) == hashlib.sha256(url.encode()).hexdigest()


def test_source_domain_extracts_hostname() -> None:
    assert source_domain("https://sub.example.com/x/y.jpg") == "sub.example.com"
    assert source_domain("not a url") == "unknown"
