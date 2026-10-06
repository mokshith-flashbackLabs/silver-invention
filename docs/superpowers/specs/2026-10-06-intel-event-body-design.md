# An event's "what it means for you" — design

*2026-10-06. Owner request, after approving a YouTube protection on dev: the person's score went
84 → 87 and their history said only "A protection against likeness misuse now counts toward your
score". "The reasoning should be good", "also for threat events and everything".*

## What changes

Every threat event and protection event the model proposes carries a `body`: one or two plain
sentences, at most 400 characters, written to the person it reaches, saying what it means for them.
The operator sees it beside the title before approving and may rewrite or clear it. The approved
value is stored on the event (`threat_events.body`, `protection_events.body`, both columns that
existed and were always `''` for an intel event), so `svc.v_active_scoped_events.body` carries it
to the backend, which shows it in the person's score history.

This replaces the 2026-09-30 note "a threat proposal carries only a title" and its protection twin.

## Where

| Piece | Change |
|---|---|
| `intel/schemas.py` | `ProposedThreatEvent.body` and `ProposedProtectionEvent.body`, REQUIRED in the structured output so the model is made to write one |
| `intel/prompts.py` | a `body` instruction for each kind; `PROPOSE_PROMPT_VERSION` → `propose-v5` |
| `intel/generation.py` | `_event_body`: masked and normalised like every model text. Unlike the title it never drops the proposal: empty or over 400 characters, it is LEFT OUT (`""`, counted `event_body_missing` / `event_body_too_long`), never truncated |
| `intel/proposal_models.py` | `body` on both `…Suggested` (default `""`, max 400, trimmed), so a proposal written before this still parses and approves with an empty body; `body` in both `…Values`, so an operator's edit is accepted |
| `intel/decisions.py` | the event row is inserted with the decided `body` |
| `intel/renewal.py`, `protection_store.py` | a renewal carries the credit's `body` forward unchanged |

## The words' rules (in the prompt; the operator is the check)

- Threat: what happened and what it can mean for someone exposed through those tags. Never that the
  reader's photos or anything of theirs was found, leaked or affected; nobody knows that.
- Protection: what it lets them do or what now happens by default, and any limit the evidence states.
  Never "safe" or "protected", never an outcome the evidence does not support.
- Both: no private individual, no contact details, no links.

## Not changed

Bounds, corroboration, recency, overlaps, the location attestation and the decision flow. A body is
text, not a number; it moves no score. No migration: both columns exist. The backend's history copy
(its 2026-10-06 change) is what puts the words in front of the person.

## Deploy

Services first, then the backend (which relays `values.body`). Old proposals keep working with an
empty body. Events approved before this keep `''`; their history line names the event but carries
only the backend's fixed reason.
