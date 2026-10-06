# A points change's reason — design

*2026-10-06. Owner, after the event body (`2026-10-06-intel-event-body-design.md`): "also for threat
events and everything". A published points change read "We updated how the score is calculated" for
everyone. The owner then rejected, one after another, "Points changed for your answer …", "public
<platform> …" and "We updated how we calculate the score" itself: "just the reason".*

## What changes

Every `weight_change` the model proposes carries a `body`: the REASON for the change in one plain
sentence of at most 200 characters, stated as a fact about the world, naming no platform, app,
service, website, quiz question, quiz answer or person. When the published change moves a person's
score, that sentence is the whole line in their history. The backend stores it with the change's
provenance row at publish and splits the movement by reason (its 2026-10-06 change).

## Where

| Piece | Change |
|---|---|
| `intel/schemas.py` | `ProposedWeightChange.body`, REQUIRED in the structured output |
| `intel/prompts.py` | the `body` rule for weight changes; `PROPOSE_PROMPT_VERSION` → `propose-v6` |
| `intel/generation.py` | `_event_body(..., limit=MAX_WEIGHT_REASON_CHARS)`: masked, normalised, left out (`""`, counted) when empty or over 200, never truncated, the proposal kept |
| `intel/proposal_models.py` | `WeightDelta.body` (default `""`, max 200, trimmed): `suggested` and `decided` |
| `intel/decisions.py` | the operator's `values` now MERGE over `suggested` (they replaced it): a delta-only edit keeps the reason, a reason-only edit keeps the delta |

## Not changed

Bounds, corroboration, the one-approved-per-cell rule, publish, the acknowledgement. No migration on
this side. A proposal written before this has `body: ""` and publishes with no reason; its line keeps
the backend's generic copy.

## Deploy

Services first (the backend relays `values.body`, and reads `decided.body` at publish). Order with the
backend's migration 0076 does not matter for correctness: an older backend ignores the field.
