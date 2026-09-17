"""The floors, published.

One read. It exists because neither repo could verify the other's numbers.
``ATTRIBUTION_MAX_CANDIDATES`` on the proxy side documents our floor and
enforces nothing; ``MIN_DISCOVERY_AGE`` is carried independently in both repos,
so if v2 moves it, the ``subject_is_adult`` boolean on
``POST /v1/liveness/{sid}/result`` means something different on each side of the
boundary and nothing detects that. The proxy was asked to assert against this
endpoint at boot and refuse to start on a mismatch, which would have converted a
silent divergence into a failed deploy. **It never did** — confirmed by the
backend team on 2026-09-16. Nothing reads this endpoint today, so a divergence
is caught only by a person comparing two task definitions, which is how the
92-versus-99 attribution split was in fact found.

**Read straight from config, never from a constant declared here.** A constant
would be a second copy of each number, and it would start lying the moment
somebody edits one and not the other — the exact class of drift this endpoint
exists to catch. There is nothing to test about the values; the test worth
having is that changing config changes the response.

Not admin-gated, and still behind ``X-Service-Token`` like every other route.
The original reason — the proxy needs these on every boot, so requiring the admin
token would put that token in the proxy's ordinary runtime environment — no
longer applies, because the proxy does not call this at boot or at all. What
remains is a weaker but sufficient reason: four policy numbers already documented
in ``INVARIANTS.md``, disclosed to a caller that already holds the service token.
Admin-gating is now available without the boot-dependency cost, and is a decision
somebody could take; it has not been taken.
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
