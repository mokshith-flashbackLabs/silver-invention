"""Startup logs the AWS account and region, loudly.

Step 9's done-when. Liveness needs real Rekognition, so there is no local-only
mode for the identity path: a developer runs against a real AWS account by
necessity, and the only thing separating dev from prod is which credentials
happen to be in the shell. The failure is silent in both directions —
test faces indexed into the production collection, or a production deploy
pointed at a dev collection where every search comes back empty.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import structlog

from imageshield.aws_identity import log_aws_identity


class FakeSts:
    def __init__(self, response: Any) -> None:
        self._response = response

    def get_caller_identity(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _captured(**kwargs: Any) -> list[dict[str, Any]]:
    structlog.configure(
        processors=[structlog.testing.LogCapture()],
        wrapper_class=structlog.make_filtering_bound_logger(0),
    )
    capture = structlog.get_config()["processors"][0]
    log_aws_identity(**kwargs)
    entries: list[dict[str, Any]] = capture.entries
    return entries


def test_the_account_and_region_are_logged() -> None:
    entries = _captured(
        region="us-east-1",
        collection_id="identity-v1",
        client=FakeSts({"Account": "225989356895", "Arn": "arn:aws:iam::225989356895:user/dev"}),
    )

    (entry,) = [e for e in entries if e["event"] == "aws.identity"]
    assert entry["account"] == "225989356895"
    assert entry["region"] == "us-east-1"
    assert entry["collection_id"] == "identity-v1"


def test_it_is_a_warning_so_it_survives_a_scrolling_console() -> None:
    """INFO in a service that logs every request at INFO is not noticed. This
    line exists to be seen at the moment somebody realises they have the wrong
    shell open."""
    entries = _captured(
        region="us-east-1",
        collection_id="identity-v1",
        client=FakeSts({"Account": "1", "Arn": "arn:aws:iam::1:user/dev"}),
    )

    (entry,) = [e for e in entries if e["event"] == "aws.identity"]
    assert entry["log_level"] == "warning"


def test_missing_credentials_warn_and_never_block_boot() -> None:
    """Not knowing which account you are on is worth a warning, not an outage —
    but it IS worth a warning, because every Rekognition call is about to fail
    and the reason would otherwise surface as an unrelated 503."""
    from botocore.exceptions import NoCredentialsError

    entries = _captured(
        region="us-east-1", collection_id="identity-v1", client=FakeSts(NoCredentialsError())
    )

    assert [e["event"] for e in entries] == ["aws.identity_unknown"]


def test_the_app_lifespan_actually_calls_it() -> None:
    """PERMANENT. This wiring was reverted once by a stray `git checkout` during
    an unrelated verification and nothing failed — the helper was tested, the
    call site was not. Asserted against the source so it cannot vanish again
    without a test going red.
    """
    app_py = Path(__file__).resolve().parents[1] / "src" / "imageshield" / "http" / "app.py"
    tree = ast.parse(app_py.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "log_aws_identity" in called, "app.py no longer logs the AWS account at startup"
