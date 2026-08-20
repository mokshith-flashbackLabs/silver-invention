# Backend task — serve the protection score surface (paste into a backend-repo session)

Everything below reads from `svc` views that are **live in dev now** (migrations 0021–0023
applied 2026-08-20; `/readyz` validates the views and your role's SELECT grants). Your
connection roles (`app_backend` / `imageshield_proxy` members) already inherit
`imageshield_proxy_ro` — no new grants needed.

---

## The prompt

> Add the protection-score surface to the backend. The ImageShield services repo now exposes
> four new read-only views in the shared database's `svc` schema; build the user-facing reads
> on top of them, following our existing pattern for `v_person_hits`/`v_person_report_summary`.
> **Never compute, adjust, or cache-derive a score in this repo** — the score has exactly one
> writer on the services side, and our history with a second score implementation is the −18
> bug. Serve what the views say, verbatim.
>
> ### Reads to implement (one route each, our usual auth/session middleware)
>
> 1. **Score card** — `SELECT score, components, config_version, computed_at FROM
>    svc.v_person_score WHERE person_ref = $1`. No row → render the "setting up" state (a row
>    appears after the user's first enrolment/seed/scan or the hourly tick; never fabricate 0
>    or 100). `components` is jsonb `{posture, coverage, exposure, threat}` (ints; out of
>    40/25/25/10).
> 2. **Score history feed** — `SELECT delta, component, cause_kind, cause_ref, score_after,
>    created_at FROM svc.v_person_score_events WHERE person_ref = $1 ORDER BY score_event_id
>    DESC LIMIT 50`. Map `cause_kind` to copy: `feedback`→"you responded to a match",
>    `enrolment`→"you completed setup", `seed_registered`→"you added photos",
>    `run_completed`→"a scan finished", `review_decision`→"a match was confirmed by review",
>    `threat_event`→"a threat affecting you was reported", `threat_retracted`→"a threat report
>    was withdrawn", `tick`→"periodic re-check". Show `delta` signed.
> 3. **Recommendations (action cards)** — `SELECT rec_id, kind, params, status, created_at,
>    expires_at FROM svc.v_person_recommendations WHERE person_ref = $1 AND status = 'open'`.
>    Kinds: `complete_enrolment`, `add_seed_photos` (params: target/have), `refresh_seeds`
>    (params: fresh_days), `respond_to_hits` (params: count), `run_priority_scan` (params:
>    event_id; the CTA triggers our existing scan flow). Completion is detected server-side
>    from data — build NO "mark done" affordance.
> 4. **Threat context banner** — `SELECT event_id, kind, title, body, severity, starts_at,
>    expires_at FROM svc.v_person_threat_context WHERE person_ref = $1`. Rows exist only for
>    users the event actually matched; render as context ("a leak was reported at a site where
>    your content appears"), never as a global news ticker.
> 5. **Hits list update** — `svc.v_person_hits` gained three columns: `confirm_state`,
>    `severity`, `decided_at`. Only `confirm_state = 'confirmed'` may be presented to the user
>    as a finding (severity drives urgency copy: `ncii_suspected` ≫ `benign_copy`); everything
>    else renders, at most, as "being checked". Quarantined/duplicate rows never appear in the
>    view at all — build nothing for them.
>
> ### Copy rules (safety, non-negotiable)
> - The score is "protection" / "likeness health" in *monitored sources* — never "you're safe",
>   never "across the web", and 100 never renders as all-clear.
> - If the UI shows a band label next to the number (e.g. "low"), the label must name its noun
>   ("low risk"), and the score→label thresholds live in ONE shared constant.
> - Responding to a match can only ever improve or hold the user's number — if any UI math
>   implies otherwise, it's a bug against services invariant #45.
>
> ### Explicitly out of scope for this repo
> Computing/altering scores; writing to any of these views' base tables; admin/review/threat
> WRITE operations (those are the admin-panel task — see the services repo's
> `docs/ADMIN_PANEL_INTEGRATION.md`, whose rule 0 routes the panel through this backend).

---

Column authority: `PROXY_INTEGRATION.md` §6 in the services repo. If a needed column is
missing, ask the services side to ADD it (additive is free); never work around with a base
table.
