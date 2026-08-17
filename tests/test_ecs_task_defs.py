"""The dev task definitions, asserted from the JSON that ECS will register.

Same reasoning as tests/test_iam_policy.py: a policy asserted from a copy of the
intent is asserted about nothing. These parse the actual files.

The handoff's §10 checklist is mechanically checkable, so it is checked here
rather than in a reviewer's head.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ECS_DIR = Path(__file__).resolve().parents[1] / "infra" / "ecs"
SERVICES_TASK = ECS_DIR / "imageshield-dev-services.json"
MIGRATE_TASK = ECS_DIR / "imageshield-dev-migrate-services.json"
TASK_ROLE = ECS_DIR / "policies" / "services-task-role.json"

LIVENESS_BUCKET = "imageshield-dev-liveness-225989356895"


def _load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _statements(path: Path) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = _load(path)["Statement"]
    return statements


def _actions(path: Path) -> list[str]:
    actions: list[str] = []
    for statement in _statements(path):
        raw = statement.get("Action", [])
        actions.extend([raw] if isinstance(raw, str) else raw)
    return actions


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK, TASK_ROLE])
def test_the_files_exist_and_parse(path: Path) -> None:
    assert path.is_file(), f"{path} is missing"
    assert _load(path)


def test_the_task_role_can_only_read_s3_and_only_the_liveness_bucket() -> None:
    """This service writes to S3 through presigned URLs from the proxy
    (CLAUDE.md §3.3). GetObject on the liveness bucket is the one read it needs;
    any Put/Delete/List would make the presigned handshake optional, and an
    optional handshake is one that gets skipped."""
    for action in _actions(TASK_ROLE):
        if not action.lower().startswith("s3:"):
            continue
        assert action == "s3:GetObject", f"unexpected S3 action: {action}"

    for statement in _statements(TASK_ROLE):
        raw = statement.get("Action", [])
        actions = [raw] if isinstance(raw, str) else raw
        if not any(a.lower().startswith("s3:") for a in actions):
            continue
        resources = statement["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        for resource in resources:
            assert LIVENESS_BUCKET in resource, f"S3 grant outside liveness: {resource}"
            assert resource != "*", "S3 grant is unscoped"


# AWS does not support resource-level scoping for these three at all — a
# Face Liveness session is not a collection operation, so `Resource: "*"` is
# the only grant IAM accepts for them. That makes them the one legitimate
# exception to "no rekognition wildcard": every OTHER rekognition action below
# (IndexFaces, SearchFacesByImage, ...) still must never see `Resource: "*"`.
_UNSCOPABLE_REKOGNITION_ACTIONS = {
    "rekognition:createfacelivenesssession",
    "rekognition:startfacelivenesssession",
    "rekognition:getfacelivenesssessionresults",
}


def test_no_rekognition_action_is_unscoped() -> None:
    """Scoped to the two dev collection ARNs. `Resource: "*"` on rekognition
    would let this role reach a production collection from dev.

    Face Liveness's three session actions are excluded: they take no
    resource type in IAM, so `Resource: "*"` there is not the over-broad
    grant it would be for a collection-shaped action like IndexFaces.
    """
    for statement in _statements(TASK_ROLE):
        raw = statement.get("Action", [])
        actions = [raw] if isinstance(raw, str) else raw
        rekognition_actions = [a for a in actions if a.lower().startswith("rekognition:")]
        if not rekognition_actions:
            continue
        if all(a.lower() in _UNSCOPABLE_REKOGNITION_ACTIONS for a in rekognition_actions):
            continue
        resources = statement["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        assert "*" not in resources, "rekognition granted on Resource: *"
        for resource in resources:
            assert "collection/" in resource, f"not a collection ARN: {resource}"
            assert "-dev-" in resource, f"not a dev collection: {resource}"


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK])
def test_no_secret_arrives_through_environment(path: Path) -> None:
    """An `environment` value is visible in describe-task-definition to anyone
    with read access. Handoff §4 and §10."""
    suspicious = ("password", "secret", "token", "api_key", "apikey", "credential")
    for container in _load(path)["containerDefinitions"]:
        for entry in container.get("environment", []):
            name = entry["name"].lower()
            assert not any(word in name for word in suspicious), (
                f"{entry['name']} looks like a secret but is in `environment`"
            )
            assert "arn:aws:secretsmanager" not in entry["value"]


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK])
def test_every_task_has_both_roles(path: Path) -> None:
    """Execution role pulls the image and resolves secrets before the code runs;
    task role is what the code itself uses. Two different things, both required."""
    document = _load(path)
    assert document["executionRoleArn"]
    assert document["taskRoleArn"]


def test_services_binds_8081_in_host_mode() -> None:
    document = _load(SERVICES_TASK)
    assert document["networkMode"] == "host"
    container = document["containerDefinitions"][0]
    ports = {entry["name"]: entry["value"] for entry in container["environment"]}
    assert ports["HTTP_PORT"] == "8081"


def test_services_declares_the_stub_provider_and_matching_regions() -> None:
    """Task 1's boot assertions refuse otherwise — better to catch it here than
    in a container that will not start."""
    container = _load(SERVICES_TASK)["containerDefinitions"][0]
    env = {entry["name"]: entry["value"] for entry in container["environment"]}
    assert env["SEARCH_PROVIDER"] == "stub"
    assert env["AWS_REGION"] == env["REKOGNITION_REGION"] == "ap-south-1"
    assert env["SEARCH_MATCH_THRESHOLD"] != "80"


def test_the_migration_task_does_not_serve_http() -> None:
    """It is a one-off task; a migration container that binds 8081 would
    collide with the running service on a host-mode instance."""
    container = _load(MIGRATE_TASK)["containerDefinitions"][0]
    assert "migrate" in " ".join(container["command"])
