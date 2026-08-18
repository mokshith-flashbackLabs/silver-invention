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

from imageshield.config import Config

ECS_DIR = Path(__file__).resolve().parents[1] / "infra" / "ecs"
SERVICES_TASK = ECS_DIR / "imageshield-dev-services.json"
MIGRATE_TASK = ECS_DIR / "imageshield-dev-migrate-services.json"
WORKER_TASK = ECS_DIR / "imageshield-dev-services-worker.json"
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


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK, WORKER_TASK, TASK_ROLE])
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


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK, WORKER_TASK])
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


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK, WORKER_TASK])
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


def _container_supplied_names(container: dict[str, Any]) -> set[str]:
    """Every variable one container will actually see, from both blocks.

    Upper-cased because pydantic-settings is case-insensitive: a field named
    ``database_url`` is fed by ``DATABASE_URL``, and comparing the two forms
    directly would report every field as missing.
    """
    return {entry["name"].upper() for entry in container.get("environment", [])} | {
        entry["name"].upper() for entry in container.get("secrets", [])
    }


def _supplied_names(path: Path) -> set[str]:
    return _container_supplied_names(_load(path)["containerDefinitions"][0])


def test_every_required_config_field_is_supplied_by_the_task_definition() -> None:
    """A required field the task definition omits is a crash loop, and the only
    place it surfaces is CloudWatch — after the deploy, in a log nobody is
    watching yet. `Config` validates at boot and exits non-zero naming the key,
    which is the right behaviour and also the least visible one.

    Derived from ``Config.model_fields`` rather than transcribed, so a field
    added with no default fails here on the commit that adds it rather than on
    the deploy that ships it.

    ``database_url`` is a deliberate exception, covered separately by
    ``test_database_url_is_supplied_directly_or_via_all_five_parts`` below: it
    now carries a default (`""`) so it can be composed from `DB_HOST` /
    `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` when a deployment cannot
    publish a single `DATABASE_URL` — the dev RDS Secrets Manager secret has
    no `url` key. A default means ``is_required()`` no longer flags it here,
    which is exactly why it needs its own check instead.
    """
    required = {
        name.upper()
        for name, field in Config.model_fields.items()
        if field.is_required()
    }
    assert required, "no required fields found — model_fields introspection broke"

    missing = required - _supplied_names(SERVICES_TASK)
    assert missing == set(), (
        f"{SERVICES_TASK.name} omits required config: {sorted(missing)}."
        " The container will exit non-zero at boot with these names in its log."
    )


_DATABASE_URL_PARTS = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK, WORKER_TASK])
def test_database_url_is_supplied_directly_or_via_all_five_parts(path: Path) -> None:
    """The replacement for the generic check above, for exactly one field.

    Still fatal if a task definition supplies neither DATABASE_URL nor a
    complete set of DB_* parts, or supplies the parts incomplete — this is
    the actual mismatch this suite exists to catch: the dev secret
    (``imageshield/dev/db/app_services`` / ``migrator_services``) is
    RDS-shaped with no ``url`` key, so a ``:url::`` secret entry resolves to
    nothing and the container (or, for the migration task,
    ``scripts/migrate.py``) exits non-zero at boot naming whichever of
    DATABASE_URL/DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD is absent.
    """
    for container in _load(path)["containerDefinitions"]:
        supplied = _container_supplied_names(container)
        if "DATABASE_URL" in supplied:
            continue
        missing_parts = _DATABASE_URL_PARTS - supplied
        assert not missing_parts, (
            f"{path.name} container {container['name']!r} supplies neither"
            " DATABASE_URL nor a complete set of DB_* parts — missing"
            f" {sorted(missing_parts)}. The container will exit non-zero at"
            " boot with these names in its log."
        )


def test_the_task_definition_sets_no_variable_config_does_not_read() -> None:
    """The reverse direction, and how ATTRIBUTION_MAX_CANDIDATES=20 got in: it
    was copied from the handoff's `api` (proxy) env block rather than its
    `services` block. `extra="ignore"` means a name this service does not read is
    accepted in silence, so a variable set for the wrong container looks exactly
    like one that works.

    ENROLMENT_QUALITY_FILTER is the one documented exception: invariant #5 pins
    `QualityFilter: HIGH` in code, so the variable is inert by design and is
    present only because the handoff's env block lists it. A knob the operator
    sets that silently vanishes is worse than one documented as having no reader.
    """
    known = {name.upper() for name in Config.model_fields}
    inert_by_design = {"ENROLMENT_QUALITY_FILTER"}
    unread = _supplied_names(SERVICES_TASK) - known - inert_by_design
    assert unread == set(), (
        f"{SERVICES_TASK.name} sets variables this service does not read:"
        f" {sorted(unread)}. extra='ignore' accepts them silently."
    )


def test_attribution_max_candidates_is_not_pinned_in_the_task_definition() -> None:
    """It has a default of 5 in config and was never required here. Setting 20
    quadruples attribution's Rekognition `MaxFaces` and makes
    `GET /v1/config/floors` publish 20 — which the proxy asserts against at boot,
    so the two repos disagree about a published floor over a variable that was
    copied from the wrong block of the handoff.
    """
    assert "ATTRIBUTION_MAX_CANDIDATES" not in _supplied_names(SERVICES_TASK)


def test_the_migration_task_does_not_serve_http() -> None:
    """It is a one-off task; a migration container that binds 8081 would
    collide with the running service on a host-mode instance."""
    container = _load(MIGRATE_TASK)["containerDefinitions"][0]
    assert "migrate" in " ".join(container["command"])


def _worker_containers() -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in _load(WORKER_TASK)["containerDefinitions"]}


def test_worker_task_runs_exactly_the_relay_and_the_search_consumer() -> None:
    """One task, two processes: the outbox relay (the only outbox→SQS path)
    and the ``search:runs`` consumer. Both essential — if either dies, ECS
    restarts the task rather than running a relay whose messages nothing
    consumes, or a consumer whose queue nothing feeds."""
    containers = _worker_containers()
    assert set(containers) == {"relay", "search-worker"}
    assert containers["relay"]["command"] == ["python", "-m", "imageshield.relay"]
    assert containers["search-worker"]["command"] == [
        "python",
        "-m",
        "imageshield.search.worker",
    ]
    for container in containers.values():
        assert container["essential"] is True


def test_worker_containers_serve_no_http() -> None:
    """Neither process has an HTTP surface. On a host-network instance a bound
    port would collide with `services` on 8081, and an HTTP health check
    against a process that serves nothing is a restart loop."""
    for container in _worker_containers().values():
        assert not container.get("portMappings"), (
            f"{container['name']} maps a port it never binds"
        )
        assert "healthCheck" not in container, (
            f"{container['name']} declares a health check with nothing to probe"
        )


def test_worker_supplies_every_required_config_field_in_both_containers() -> None:
    """Both processes call ``load_config()`` — the same required set as the
    HTTP app, validated at boot, exiting non-zero on a missing name. Same
    reasoning as the services check above, per container."""
    required = {
        name.upper() for name, field in Config.model_fields.items() if field.is_required()
    }
    for name, container in _worker_containers().items():
        missing = required - _container_supplied_names(container)
        assert missing == set(), (
            f"{WORKER_TASK.name} container {name!r} omits required config:"
            f" {sorted(missing)}. The container will exit non-zero at boot."
        )


def test_worker_declares_the_stub_provider_and_matching_regions() -> None:
    """The search worker is the one process where ``SEARCH_PROVIDER`` decides
    whether real adapters get built (``build_providers``) — this task
    definition, more than any other, is where `stub` must be pinned."""
    for name, container in _worker_containers().items():
        env = {entry["name"]: entry["value"] for entry in container["environment"]}
        assert env["SEARCH_PROVIDER"] == "stub", f"{name} would build live adapters"
        assert env["AWS_REGION"] == env["REKOGNITION_REGION"] == "ap-south-1"


def test_worker_sets_no_variable_config_does_not_read() -> None:
    """Same reverse-direction check as the services task: a name Config does
    not read is accepted in silence by ``extra='ignore'``."""
    known = {name.upper() for name in Config.model_fields}
    inert_by_design = {"ENROLMENT_QUALITY_FILTER"}
    for name, container in _worker_containers().items():
        unread = _container_supplied_names(container) - known - inert_by_design
        assert unread == set(), (
            f"{WORKER_TASK.name} container {name!r} sets variables this service"
            f" does not read: {sorted(unread)}."
        )
