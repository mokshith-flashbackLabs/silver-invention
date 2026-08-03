from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from imageshield.types import (
    parse_provider_id,
    parse_session_id,
    parse_url_hash,
    parse_user_ref,
)


class TestUuidIdentifiers:
    def test_accepts_uuid_string(self) -> None:
        value = uuid4()
        assert parse_user_ref(str(value)) == value
        assert parse_session_id(str(value)) == value

    def test_accepts_uuid_instance(self) -> None:
        value = uuid4()
        assert parse_user_ref(value) == value

    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "1234567890", "+91 98765 43210"])
    def test_rejects_non_uuid(self, bad: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            parse_user_ref(bad)
        # The invalid input (possibly a phone number) must not be echoed.
        assert bad == "" or bad not in str(excinfo.value)

    def test_result_is_a_real_uuid(self) -> None:
        assert isinstance(parse_user_ref(str(uuid4())), UUID)


class TestProviderId:
    def test_accepts_slug(self) -> None:
        assert parse_provider_id("hive_media_search") == "hive_media_search"

    @pytest.mark.parametrize("bad", ["", "Hive", "1hive", "hive!", "h", "x" * 65])
    def test_rejects_non_slug(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_provider_id(bad)


class TestUrlHash:
    def test_accepts_sha256_hex_and_normalises_case(self) -> None:
        digest = "A" * 64
        assert parse_url_hash(digest) == "a" * 64

    @pytest.mark.parametrize("bad", ["", "a" * 63, "a" * 65, "g" * 64])
    def test_rejects_non_sha256(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_url_hash(bad)
