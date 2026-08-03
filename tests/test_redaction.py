from __future__ import annotations

import json

import pytest

from imageshield.redaction import PHONE_REDACTED, redact_phone_shapes, redact_string


@pytest.mark.parametrize(
    "text",
    [
        "+91 98765 43210",
        "(555) 123-4567",
        "call me at 9876543210 tomorrow",
        "+1-800-555-0199",
        "0044 20 7946 0958",
    ],
)
def test_phone_shapes_are_redacted(text: str) -> None:
    redacted = redact_string(text)
    assert PHONE_REDACTED in redacted
    digits = [ch for ch in redacted.replace(PHONE_REDACTED, "") if ch.isdigit()]
    assert len(digits) < 7


@pytest.mark.parametrize(
    "text",
    [
        "0b6ad478-9c2f-4f0e-9c39-52a1b28ab9c8",  # UUID (user_ref) must survive
        "2026-08-03",  # ISO date
        "2026-08-03 12:47:01",  # ISO datetime
        "expires_at=2026-08-03T12:47:01Z",
        "run 42 finished",  # short numbers
        "similarity 0.9231",
    ],
)
def test_legitimate_values_survive(text: str) -> None:
    assert redact_string(text) == text


def test_processor_redacts_nested_values() -> None:
    event = {
        "event": "enrolment failed for +91 98765 43210",
        "detail": {"contact": "(555) 123-4567", "user_ref": "0b6ad478"},
        "items": ["+1-800-555-0199", "safe"],
        "count": 3,
    }
    redacted = redact_phone_shapes(None, "info", event)
    text = json.dumps(redacted)
    assert "98765" not in text
    assert "123-4567" not in text
    assert "800-555-0199" not in text
    assert redacted["count"] == 3
    assert "safe" in text


def test_end_to_end_log_line_carries_no_phone(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    from imageshield.http.logging import configure_logging

    configure_logging()
    structlog.get_logger("test").info(
        "boundary breach", supplied_value="+91 98765 43210"
    )
    out = capsys.readouterr().out
    assert "98765" not in out
    assert PHONE_REDACTED in out
