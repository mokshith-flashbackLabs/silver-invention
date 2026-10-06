# A suggested threat's recommended action — design

*2026-10-06. Owner: "when a threat event is suggested will it come with recommendation instructions
links and things like that", then "build it". Until now an AI-suggested threat carried no action; a
hand-created one could attach one at creation (backend 0056), and completing it gives the person the
threat's points back.*

## What changes

A threat proposal may carry `suggested.action`, drafted by the model, in exactly the shape the
backend stores for a hand-written action: `title` (≤120), `why` (≤300), `steps` (1–10, each ≤300),
optional `link_url` (https, ≤500) and `link_label` (≤60, only with a link). The operator sees it,
may replace it whole or remove it (`values.action`, `null` removes), and on approval the backend
attaches `decided.action` to the new event exactly as it attaches a hand-created threat's action.
Services never store an action on the event: actions are the backend's (its 0056).

## The two rules

1. **Only a real step.** The prompt asks for an action only when the evidence names something a
   person exposed through the tags can actually do and finish (2FA, a password change, revoking apps,
   a privacy setting); otherwise `null`. Never generic advice, never "safe", never "contact us".
2. **A link is one of the threat's own cited pages.** The model can invent a plausible URL, and this
   one reaches a victim. Each evidence item now shows the model its page URL (`url`, the document's
   final URL), and generation keeps a link only when it is https and its canonical URL hash is one of
   the cited signals' document keys. Any other link is dropped with its label; the action is kept.

## Where

| Piece | Change |
|---|---|
| `intel/bounds.py` | the action bounds, equal to the backend's `threatActionBody` |
| `intel/schemas.py` | `ProposedThreatAction`; `ProposedThreatEvent.action`, optional |
| `intel/prompts.py` | `PromptSignal.url`; the `action` rules; `propose-v6` → `propose-v7` |
| `intel/generation.py` | `_threat_action`: masked, normalised, out of bounds drops the ACTION never the threat (`action_dropped_out_of_bounds`), uncited link dropped (`action_link_not_cited`); an absent link is absent, never null |
| `intel/proposal_models.py` | `ThreatActionSuggested`; `action` on `ThreatEventSuggested` (default none) and `ThreatEventValues`; `ContextSignal.document_url` |
| `intel/proposal_store.py` | the context read carries `d.final_url AS document_url` |

No migration. A proposal written before this has no action and approves exactly as before.

## Deploy

Services first, then the backend (it attaches `decided.action` and relays `values.action`).
