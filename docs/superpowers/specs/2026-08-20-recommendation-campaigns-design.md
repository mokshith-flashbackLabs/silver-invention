# Recommendation campaigns + the console ideas panel — design addendum

**Date:** 2026-08-20
**Status:** approved in discussion; NOT built — next push
**Extends:** `2026-08-19-protection-score-design.md` (the score/recommendations system it plugs into)

## 1. What this adds

Two layers on top of the five fixed code-kind recommendations:

1. **Operator-authored campaigns** — ops mint new recommendation content from the control room
   (canonical example: *"upload a photo of yourself from 5+ years ago — older photos of you
   circulate too, and image search can only find copies of what we hold"*). Campaigns are
   content + audience + completion rule; they plug into the EXISTING score mechanics and add no
   new arithmetic.
2. **An LLM ideas panel in the console** — drafts candidate campaigns for operators. The LLM is
   a drafting assistant for staff, full stop.

## 2. Decisions taken (user, 2026-08-20)

| Question | Decision |
|---|---|
| Completion detection for campaigns | **Flow provenance**: the app's upload flow, entered from a recommendation card, tags the new seed with the recommendation's id (proxy passes `recommendation_id` on `POST /v1/seeds`). Completion = a seed exists carrying that tag. Earned, never claimed. |
| LLM route | **Anthropic API direct** from the console deployable only; `ANTHROPIC_API_KEY` as a new key in Secrets Manager. Services deployables stay LLM-free. |
| Timing | Spec now, build next push (after the current push's dev-deploy prerequisites are done). |

## 3. The hard rules (these are the design)

- **The LLM never reaches a user and never moves a point.** Its output becomes visible to users
  only after a named operator edits/approves the exact text, and the approved campaign is then
  assigned by deterministic code. This is CLAUDE.md §10's "LLM at the periphery" made concrete.
- **Prompts carry no per-user data.** The ideas panel sends only the operator's typed goal and
  aggregate, non-identifying context (e.g. "many users have seed portfolios older than 90
  days"). Never a `user_ref`, never a hit URL, never a domain list tied to a person.
- **Score arithmetic is untouched.** Campaign recommendations participate only through the
  existing mechanics: they appear as open recommendations, age into the posture decay
  (`score_rec_soft_age_days`), complete via provenance, and respect dismissal-blocks. No new
  weights, no boot-validation changes, no per-campaign point values an operator could inflate.
- **Audience targeting is a fixed deterministic menu**, never free-form and never LLM-evaluated:
  `all_users` | `seed_count_below(N)` | `seeds_older_than_days(N)` | `has_live_hits_on(domains[])`.
  Each is one indexed SQL predicate.
- **Age-sensitive copy guard:** campaign copy asking for photos must say *photos of yourself* —
  enrolment is 13+, subjects own their own likeness history; the ideas panel's system prompt and
  the operator checklist both state it.

## 4. Schema (next migration pair)

```
recommendation_campaigns (
  campaign_id  UUID PK,
  title        TEXT NOT NULL,          -- user-facing card title
  body         TEXT NOT NULL,          -- user-facing card copy
  audience     JSONB NOT NULL,         -- {"rule": "seed_count_below", "n": 5} etc., fixed menu
  status       TEXT CHECK (draft|active|ended) DEFAULT 'draft',
  llm_drafted  BOOLEAN NOT NULL,       -- provenance of the copy, for audit honesty
  created_by   TEXT NOT NULL,          -- operator
  approved_by  TEXT,                   -- operator; NOT NULL required for status='active' (CHECK)
  starts_at / ends_at TIMESTAMPTZ,
  created_at / updated_at
)
recommendations: + campaign_id UUID NULL REFERENCES recommendation_campaigns
  + kind CHECK gains 'campaign'
  + CHECK ((kind = 'campaign') = (campaign_id IS NOT NULL))
search_seeds: + recommendation_id UUID NULL REFERENCES recommendations(rec_id)
```

Assignment: the recompute's existing sync pass also desires one open `campaign` rec per active
campaign whose audience predicate matches the user (dismissed-block and open-uniq semantics
reused verbatim). Ending a campaign expires its open recs.

Completion: an open campaign rec completes when a `search_seeds` row exists with
`recommendation_id = rec_id`.

## 5. Contract additions

- **Proxy** (`PROXY_INTEGRATION.md`): `POST /v1/seeds` gains optional `recommendation_id: UUID`.
  The proxy passes it only when the upload flow was entered from a recommendation card. Additive.
- **Admin API:** `GET/POST /v1/admin/campaigns`, `POST /v1/admin/campaigns/{id}/activate`
  (records `approved_by`), `POST /v1/admin/campaigns/{id}/end`. Existing dual-token auth,
  operator in body, audit rows.
- **svc views:** `v_person_recommendations` already carries `kind`/`params`; add `campaign_id`,
  `title`, `body` (additive columns) so the proxy can render campaign cards without a new read.

## 6. The ideas panel (console only)

Operator types a goal → console backend calls the Anthropic API (latest Claude model, id pinned
in `ConsoleConfig`, key from Secrets Manager) with a system prompt constraining output to
structured drafts: `{title, body, audience: <one of the fixed menu>, rationale}` × N. Drafts
render as editable forms; **Save creates a `draft` campaign; a second operator action activates
it.** Failures degrade to the blank manual form — the panel is an accelerator, not a dependency.
Implementation must load the repo's `claude-api` reference for current model ids/SDK usage at
build time rather than pinning one here.

## 7. Invariant candidate (#48, to land with the build)

An LLM's output never reaches an end user and never influences a score without a named operator
approving the exact text; LLM calls exist only in the console deployable; prompts carry no
per-user data. Enforced by: an import gate (no `anthropic` import outside `console/`), the
`approved_by` CHECK, and a prompt-construction unit test asserting no `user_ref`/URL fields.

## 8. Out of scope

Per-user LLM personalization of copy; LLM-chosen audiences; auto-activation; campaign A/B
testing; push-notification delivery (digest rules stand, INVARIANTS #24).
