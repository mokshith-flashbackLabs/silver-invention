"""Log which AWS account and region this process is pointed at, loudly.

Step 9's done-when: *"Someone will eventually run this against production by
accident; make it loud."*

That is not hypothetical here. Liveness needs real Rekognition, so there is no
local-only mode for the identity path — a developer runs against a real AWS
account by necessity, and the only thing distinguishing dev from prod is which
credentials happen to be in the environment. The failure is silent in both
directions: enrolling test faces into the production ``identity-v1``, or
pointing a production deploy at a dev collection where nobody's face is
indexed and every search comes back empty.

``sts:GetCallerIdentity`` needs no IAM permission — it is the one call every
principal may make — so this cannot fail for want of a grant, and a failure
here never blocks boot. Not knowing which account you are on is worth a
warning, not an outage.
"""

from __future__ import annotations

from typing import Any

# TID251 per-file ignore (pyproject.toml): STS GetCallerIdentity only.
import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

log = structlog.get_logger("imageshield.aws")


def log_aws_identity(*, region: str, collection_id: str, client: Any | None = None) -> None:
    sts = client if client is not None else boto3.client("sts", region_name=region)
    try:
        identity = sts.get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        # No credentials, or an expired session. Loud, because every
        # Rekognition call this process makes is about to fail and the reason
        # would otherwise surface as an unrelated 503.
        log.warning(
            "aws.identity_unknown",
            region=region,
            collection_id=collection_id,
            detail=str(exc),
        )
        return

    log.warning(
        # WARNING, not INFO, and deliberately. This line exists to be noticed in
        # a scrolling console at the moment somebody realises they have the
        # wrong shell open. An INFO line in a service that logs every request at
        # INFO is not noticed.
        "aws.identity",
        account=str(identity.get("Account", "unknown")),
        region=region,
        collection_id=collection_id,
        arn=str(identity.get("Arn", "unknown")),
    )
