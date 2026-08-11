"""The IAM policy, asserted from the artifact Terraform actually applies.

Step 9's done-when calls this *"the single most important assertion in the
step"*: **the role has no `s3:*` permissions of any kind.**

The test parses ``infra/terraform/policies/service-role.json`` — the same file
``templatefile()`` renders — rather than a copy of the intent. A policy
asserted from a duplicate is asserted about nothing.

This is one of only three places where the data boundary is enforced by
something other than discipline; the other two are the schema lint (step 2) and
the structlog redaction processor (step 1). Code review catches the S3 client
someone writes. Only the missing grant catches the one nobody reviews.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "terraform"
    / "policies"
    / "service-role.json"
)


def _policy() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    return document


def _actions() -> list[str]:
    actions: list[str] = []
    for statement in _policy()["Statement"]:
        raw = statement.get("Action", [])
        actions.extend([raw] if isinstance(raw, str) else raw)
    return actions


def test_the_policy_file_exists_and_parses() -> None:
    """If this file moves or stops being JSON, every assertion below would
    otherwise vanish silently."""
    assert POLICY_PATH.is_file(), f"{POLICY_PATH} is missing"
    assert _policy()["Statement"], "policy has no statements"


def test_the_role_has_no_s3_permission_of_any_kind() -> None:
    """THE assertion. Not even GetObject.

    CLAUDE.md §3.3: the presigned-URL handshake exists precisely so this
    service never needs S3 credentials. Grant it one and the handshake becomes
    optional, which means one day it will be skipped.
    """
    offenders = [action for action in _actions() if action.lower().startswith("s3:")]
    assert offenders == [], (
        f"the service role grants {offenders}. It must grant NO s3 action —"
        " see CLAUDE.md §3.3 and the comment in the policy file."
    )


def test_no_statement_grants_a_wildcard_action() -> None:
    """`"Action": "*"` would include s3:* without the string "s3" appearing
    anywhere — the exact shape the test above would miss."""
    offenders = [action for action in _actions() if action == "*" or action.endswith(":*")]
    assert offenders == []


def test_no_statement_is_a_deny_that_could_mask_a_grant() -> None:
    """Every statement is an Allow. A Deny here would be a sign somebody added
    a broad grant and tried to fence it, which is not how this policy works."""
    effects = {statement["Effect"] for statement in _policy()["Statement"]}
    assert effects == {"Allow"}


@pytest.mark.parametrize(
    "action",
    [
        # Liveness (step 3) and enrolment (step 4).
        "rekognition:CreateFaceLivenessSession",
        "rekognition:GetFaceLivenessSessionResults",
        "rekognition:IndexFaces",
        # The deletion path (INVARIANTS #7: DeleteFaces, verify, then tombstone).
        "rekognition:DeleteFaces",
        "rekognition:ListFaces",
        # Attribution (task 05, INVARIANTS #1a).
        "rekognition:DetectFaces",
        "rekognition:SearchFacesByImage",
        # The outbox relay and the search worker.
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
    ],
)
def test_every_call_the_code_actually_makes_is_granted(action: str) -> None:
    """The opposite failure to the one above: a policy so tight the service
    cannot work. Each entry corresponds to a call in src/."""
    assert action in _actions(), f"{action} is used by src/ but not granted"


def test_secrets_are_scoped_to_arns_not_star() -> None:
    """`secretsmanager:GetSecretValue` on `*` reads every secret in the account,
    including ones belonging to other systems."""
    (statement,) = [
        s for s in _policy()["Statement"] if "secretsmanager:GetSecretValue" in s["Action"]
    ]
    assert statement["Resource"] != "*"


def test_collection_operations_are_scoped_to_the_collection() -> None:
    """Where the API permits resource-level scoping, use it. IndexFaces into
    somebody else's collection is not a call this service should be able to
    make."""
    (statement,) = [
        s for s in _policy()["Statement"] if "rekognition:IndexFaces" in s["Action"]
    ]
    assert ":collection/" in statement["Resource"]


def test_search_faces_by_image_is_scoped_to_the_identity_collection() -> None:
    """Attribution's one permitted face search (INVARIANTS #1a) can only ever
    reach `identity-v1`. Even if the narrowed grep gate were removed, the grant
    would not reach another collection."""
    (statement,) = [
        s
        for s in _policy()["Statement"]
        if "rekognition:SearchFacesByImage" in s["Action"]
    ]
    assert "${collection_id}" in statement["Resource"]


def test_the_document_has_only_keys_iam_accepts() -> None:
    """An IAM policy document accepts Version, Id and Statement, and AWS
    rejects the WHOLE document if anything else appears at the top level. An
    earlier draft of this file carried a "Comment" key explaining the s3
    absence; it would have failed at apply time, in a deploy, with a message
    about malformed JSON rather than about the comment.

    The explanation lives in policies/README.md instead.
    """
    assert set(_policy()) <= {"Version", "Id", "Statement"}


def test_no_resource_is_a_template_list_interpolated_into_a_string() -> None:
    """`"Resource": "${some_list}"` renders as a STRING containing JSON, not a
    list, and AWS rejects it. Every Resource here is either a literal string, a
    list of strings, or a single interpolated ARN.
    """
    for statement in _policy()["Statement"]:
        resource = statement["Resource"]
        if isinstance(resource, str):
            assert resource.count("${") <= 4, f"suspicious template in {resource}"
        else:
            assert all(isinstance(item, str) for item in resource)
