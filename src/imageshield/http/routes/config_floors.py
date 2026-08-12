"""The floors, published.

One read. It exists because neither repo could verify the other's numbers.
``ATTRIBUTION_MAX_CANDIDATES`` on the proxy side documents our floor and
enforces nothing; ``MIN_DISCOVERY_AGE`` is carried independently in both repos,
so if v2 moves it, the ``subject_is_adult`` boolean on
``POST /v1/liveness/{sid}/result`` means something different on each side of the
boundary and nothing detects that. The proxy asserts against this endpoint at
boot and refuses to start on a mismatch, which converts a silent divergence into
a failed deploy.

**Read straight from config, never from a constant declared here.** A constant
would be a second copy of each number, and it would start lying the moment
somebody edits one and not the other — the exact class of drift this endpoint
exists to catch. There is nothing to test about the values; the test worth
having is that changing config changes the response.

Not admin-gated. It publishes four policy numbers already documented in
``INVARIANTS.md``, and the proxy needs them on every boot — putting the admin
token on a boot-time dependency means the admin token lives in the proxy's
normal runtime environment, which is a worse trade than the disclosure.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends

from imageshield.config import Config
from imageshield.http.auth import require_service_token
from imageshield.http.deps import get_config
from imageshield.http.models import FloorsResponse

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])

# Two decimal places, matching attribution_runs.match_threshold's NUMERIC(5,2).
# Via str() rather than float(): Decimal(92.0) carries the binary
# representation, Decimal("92.0") carries the number that was written down.
_THRESHOLD_SCALE = Decimal("0.01")


@router.get("/config/floors")
async def get_floors(cfg: Config = Depends(get_config)) -> FloorsResponse:
    return FloorsResponse(
        min_discovery_age=cfg.min_discovery_age,
        min_enrolment_age=cfg.min_enrolment_age,
        attribution_max_candidates=cfg.attribution_max_candidates,
        attribution_match_threshold=str(
            Decimal(str(cfg.attribution_match_threshold)).quantize(_THRESHOLD_SCALE)
        ),
    )
