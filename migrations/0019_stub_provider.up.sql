-- Registers the `stub` provider — DISABLED — so `SEARCH_PROVIDER=stub` has a
-- row to dispatch against, the way `providers.enabled` already carries one
-- for `hive` and `google` (0004).
--
-- THE BUG THIS CLOSES. `search/stub.py` (StubSearchProvider) is the adapter
-- SEARCH_PROVIDER=stub builds, but `providers` held only hive and google.
-- Both the dispatch guard and `search_runs.providers_attempted` read off
-- `providers.enabled`, so a dev run under SEARCH_PROVIDER=stub attempted
-- 'hive' and 'google' — the only enabled ids — while build_providers()
-- constructs ONLY {'stub': StubSearchProvider()} under that setting. Neither
-- attempted id had a matching adapter, so every provider_calls row came back
-- error_detail='no adapter registered for this provider': the run succeeded
-- against nothing, and the switch that was supposed to make dev safe made it
-- silently useless instead.
--
-- WHY enabled = false, NOT true. This is the safety-critical choice, not a
-- default left alone. `svc.v_person_report_summary.monitored_sources` (0016)
-- counts providers that SUCCEEDED and are ENABLED — an enabled stub would let
-- a completed run claim a source that was never actually searched, which is
-- exactly the false claim CLAUDE.md §7.5 names ("'We monitor 2 sources'
-- while one has an open breaker is a false claim"). A stub that could count
-- toward coverage would be worse than the bug it replaces. Environments that
-- want the stub dispatched turn this on deliberately, by hand — not by
-- migration.
--
-- WHY calibrated = false. CLAUDE.md §7.3: an uncalibrated provider reaches
-- `review` only, never `auto_confirm` and never `drop`. The stub returns zero
-- matches by construction, so nothing is ever banded either way today — but
-- the row must not be the thing that WOULD let it alarm someone if that ever
-- changed by accident.
--
-- WHY score_domain is NULL, not a range. Hive and Google both declare a real
-- score_domain (0004) because both occasionally report a score. The stub
-- never does — `matches` is always `[]` — so there is no measured domain to
-- record, and inventing one (say, copying Hive's 0.5-1.0) would be exactly
-- the fabrication CLAUDE.md §7.2 forbids an adapter from doing, applied to
-- its config row instead. It is also a second, independent guard: if
-- `calibrated` were ever hand-flipped to true, `calibration/bands.py`'s rule
-- 3c ("score_domain_unknown") still refuses to band anything above `review`
-- for a numeric provider whose domain bounds are absent.
--
-- WHY kind = 'image_search', not 'face_search'. Matches what
-- StubSearchProvider.kind declares, for the reason the adapter itself gives:
-- claiming face_search here would assert coverage the stub does not
-- provide — the exact silent-gap failure CLAUDE.md §7.1 exists to name.
-- image_search is also what Hive and Google both declare, so a kind-aware
-- orchestrator treats a stub run exactly as it treats the image-search-only
-- stack that is actually deployed.
--
-- WHY cost_per_call_usd = 0, not NULL. The stub opens no socket and calls no
-- provider (search/stub.py), so 0 is the honest figure — not "unknown".
-- CLAUDE.md §7.6 / invariant #38 says a budget set against an unknown cost
-- fails CLOSED: a provider that cannot be priced is skipped by the guard
-- rather than dispatched. A NULL here would make the one provider that is
-- free by construction refusable by its own budget check the moment anyone
-- configured a cap.
--
-- score_version identifies this row as the stub, never a real provider's
-- version string — it matches StubSearchProvider.score_version exactly so
-- the adapter and its row can never drift apart on what actually produced a
-- row.
--
-- Same seeding style as 0004: ON CONFLICT (provider_id) DO NOTHING, so this
-- migration is a no-op rather than an error against a database that already
-- carries a hand-inserted 'stub' row.
INSERT INTO providers (
  provider_id, kind, enabled, calibrated, score_version,
  cost_per_call_usd, score_kind, score_domain
)
VALUES (
  'stub', 'image_search', false, false, 'stub-no-op-v1',
  0, 'numeric', NULL
)
ON CONFLICT (provider_id) DO NOTHING;
