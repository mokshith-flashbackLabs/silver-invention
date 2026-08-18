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
        "+1 (555) 010-9999",
        "919876543210",
    ],
)
def test_phone_shapes_are_redacted(text: str) -> None:
    redacted = redact_string(text)
    assert PHONE_REDACTED in redacted
    digits = [ch for ch in redacted.replace(PHONE_REDACTED, "") if ch.isdigit()]
    assert len(digits) < 7


def test_bare_twelve_digit_account_id_is_still_redacted() -> None:
    """Deliberate over-redaction, NOT a bug to fix later. '225989356895' (this
    project's real AWS account id shape) is indistinguishable from a phone
    number by digit count alone — ITU E.164 is 7-15 digits, and this is 12.
    redaction.py's module docstring states the policy: over-redaction is
    acceptable, leaking a real phone number is not. If a future change makes
    this test fail, that is a regression in the tripwire, not a bug fix — do
    not "fix" it by allowlisting non-phone-looking digit runs.
    """
    redacted = redact_string("account 225989356895 in us-east-1")
    assert "225989356895" not in redacted
    assert PHONE_REDACTED in redacted


@pytest.mark.parametrize(
    "text",
    [
        "0b6ad478-9c2f-4f0e-9c39-52a1b28ab9c8",  # UUID (user_ref) must survive
        "2026-08-03",  # ISO date
        "2026-08-03 12:47:01",  # ISO datetime
        "expires_at=2026-08-03T12:47:01Z",
        "run 42 finished",  # short numbers
        "similarity 0.9231",
        # Regression: fractional seconds are 1-8 digits joined by '.', which
        # the candidate class already allows, so "23.456789" alone parses as
        # a phone-shaped run and used to become
        # "2026-08-17T17:57:[REDACTED:phone-shaped]Z".
        "2026-08-17T17:57:23.456789Z",
        "2026-08-17T17:57:23Z",  # no fraction, Z offset
        "2026-08-17 17:57:23+05:30",  # space separator, colon offset
        "2026-08-17T17:57:23+0530",  # numeric offset, no colon
        "request finished at 2026-08-17T17:57:23.456789Z after one retry",  # mid-string
    ],
)
def test_legitimate_values_survive(text: str) -> None:
    assert redact_string(text) == text


def test_phone_number_immediately_after_a_timestamp_is_still_redacted() -> None:
    """Guards against the timestamp fix overcorrecting: protecting an ISO span
    must not create a blind spot for whatever follows it in the same string."""
    text = "logged_in_at=2026-08-17T17:57:23Z caller=+91 98765 43210"
    redacted = redact_string(text)
    assert "2026-08-17T17:57:23Z" in redacted
    assert "98765" not in redacted
    assert PHONE_REDACTED in redacted


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


def test_processor_preserves_the_auto_added_timestamp_field() -> None:
    """The exact bug, reproduced at the processor level rather than just the
    string level: structlog's TimeStamper(fmt="iso", utc=True) runs before
    redact_phone_shapes in the configured chain and adds a 'timestamp' key
    with fractional seconds — this is what produced
    "timestamp": "2026-08-17T17:57:[REDACTED:phone-shaped]Z" in real dev logs.
    """
    event = {
        "event": "boundary breach",
        "timestamp": "2026-08-17T17:57:23.456789Z",
    }
    redacted = redact_phone_shapes(None, "info", event)
    assert redacted["timestamp"] == "2026-08-17T17:57:23.456789Z"
