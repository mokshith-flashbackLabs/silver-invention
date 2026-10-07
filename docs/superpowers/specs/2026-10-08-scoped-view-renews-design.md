# svc.v_active_scoped_events names the credit a renewal continues — design

*2026-10-08. Owner, on the protection history lines: a lowered strength should "just give the
reason, like YouTube updated its policy".*

A protection's strength changes only through a renewal (intel/renewal.py): at the old credit's
review date a new protection event takes over, possibly with a lower strength, and the approving
operator may rewrite its `body`. The backend saw that handover as two unrelated events — the old
one stopping, the new one starting — and wrote the old one "no longer counting", so the renewal's
words, the reason, never reached the person.

**Change.** Migration 0049 re-creates `svc.v_active_scoped_events` with one column APPENDED:
`renews_event_id` (uuid) — the protection's `renews_event_id`, NULL on a threat and on a credit that
renews nothing. Appending is the one change a contract view allows without a coordinated reader
change (CLAUDE.md §3); `http/svc_contract.py` lists it so readiness checks its type. The down
migration drops and re-creates the view as 0044 left it, with its grant.

**Backend.** It reads the column as REQUIRED shape, pairs a renewal with the credit it continues,
and writes the pair as one line named by the renewal; a weaker renewal reads as just its body.

**Deploy.** Services first: a backend that expects the column fails readiness on a view without it.
On the way down, the backend first.
