# Dynamic Score — evidence that is dated, independent and counted once

**Date:** 2026-10-04. **Owner review of dev, same day:** the proposal queue held 19 AI suggestions; the
machinery worked, but the evidence behind them was undated, copied and counted twice. **Owner
decisions:** three fixes, below. **Not in scope (owner: they are fine):** threats about public figures
or celebrities, protections that cover only a subgroup, the severity scale. No rule here touches
them.

Services repo only. The backend relays the proposal reads and the decision verbatim, so the new
read fields and the new error code reach the console with no backend code change beyond mapping the
code by name (`PROXY_INTEGRATION.md`, "evidence quality").

## 1. What was wrong (dev, 2026-10-03)

1. **No date on anything.** None of ~90 evidence items carried a publication date: only a feed
   entry's `published` was recorded (`pipeline.py`), never a page's or a search result's. Stories
   from 2024 ("In the coming months, we will label images…") became "new" threats with a 30–60 day
   clock, including a Meta AI feature the model itself said had been withdrawn.
2. **Echoes counted as corroboration.** The YouTube removal protection counted techradar.com,
   petapixel.com, musicbusinessworldwide.com and completemusicupdate.com, all quoting the same
   sentence of YouTube's own announcement, and google.com beside blog.youtube (one company) as two
   publishers. `uncorroborated()` counted distinct registrable domains.
3. **Duplicates and double-counting.** "YouTube likeness detection" was proposed twice as two pending
   protection_events on `{youtube}`, citing the same influencermarketinghub.com signal. And one body
   of evidence produced a lasting weight_change (+1 on X; +1 on Instagram) AND a temporary
   threat_event on the same tag, and Meta's labelling was framed both as a protection and as a reason
   to raise a weight.

### 1.1 Why the YouTube duplicate slipped through (root cause, verified on dev)

Not the document comparison: `duplicate_of` already compared documents by canonical URL hash
(`intel_documents.url_hash`), never by row id, so a page re-read by a later run was already the same
document. On dev, the second proposal (`907f336a`, a `weight_suggestion` run's generation, 17:28Z)
cited exactly ONE signal, `c34150f5` — the very signal the first proposal (`76123765`, 17:03Z) cited,
from the first run. It reached the second run's prompt as *related* evidence (same category). But the
pending candidates for duplicate detection were loaded with `tags =` the second run's OWN new
signals' tags — 40+ signals tagged facebook, instagram, snapchat, tiktok, x and none `youtube` — so
the pending `{youtube}` protection was never loaded, never shown to the model, and never compared.
Concurrency could cause the same miss (two runs both load before either writes; the worker runs
`INTEL_RUN_CONCURRENCY` = 3 at once per task).

## 2. Fix 1 — publication dates

`intel_documents.published_at` is when the PUBLISHER says the document was published, never when we
fetched it. The first plausible value wins (`intel/recency.py`, `choose_published`):

| # | Source | Where |
|---|---|---|
| 1 | a feed item's own date (unchanged) | `pipeline._feed` |
| 2 | the page's HTML metadata: `<meta property="article:published_time">`; JSON-LD `datePublished` (top level, a top-level list, or `@graph`; an article-typed node first); `itemprop="datePublished"` (a `<meta>` or a `<time datetime>`); `<meta name=…>` `date`, `pubdate`, `publish-date`, `parsely-pub-date`, `dc.date`, `dcterms.date`, in that order | the fetcher (`fetcher/extract.py`), returned on `/v1/text` as `published_at` (ISO-8601 or null), parsed leniently in `intel/fetch_client.py` |
| 3 | a web search result's `page_age` (anthropic 1.8.0's `WebSearchResultBlock.page_age`), for a page a saved search found; a relative value ("3 days ago", "yesterday") is read against the moment the search answered | `intel/model.py` (`ModelCall.search_results`), `pipeline._discover` |
| 4 | the extraction model's `published_date` (`extract-v2`): a date the TEXT states (byline, dateline), `YYYY-MM-DD` or null, accepted only if well-formed and its year appears in the document text | `intel/schemas.py`, `intel/recency.stated_date` |

Metadata beats the model. A value before 1990 or more than two days in the future
(`PUBLISHED_MIN_YEAR`, `PUBLISHED_FUTURE_SLACK_DAYS`) is dropped and the next source is tried; with
none, the document is undated. The run outcome counts the source: `published_from_feed` ·
`_metadata` · `_search` · `_text` · `_unknown`. The fetcher still holds no database credentials and
persists nothing: this is metadata read from bytes already in memory, stdlib only.

## 3. Fix 1, continued — recency

`INTEL_THREAT_RECENCY_DAYS` (new, **required, no default**; 90 in every dev and prod container):
a threat_event is for an incident whose evidence is recent.

- **The prompt (`propose-v4`).** Every signal carries `published` (`YYYY-MM-DD` or `"undated"`); the
  payload carries `today` and `threat_recency_days`; the rules say an older incident, or one the
  evidence says has ended, is not a threat (it may support a lasting weight_change, or nothing).
- **Generation** drops a threat_event whose cited evidence is all dated and older than the window
  (`proposal_dropped_evidence_stale`).
- **Approvability.** `why_not = 'evidence_stale'` for a threat_event whose ACTIVE signals are all dated
  AND all older than the window — undated evidence never makes one stale. Order: `not_decidable` ·
  `renewed_credit_ended` · `evidence_retracted` · **`evidence_stale`** · `uncorroborated` ·
  `tags_unmapped`. The decision re-checks it in its transaction and refuses
  **`409 proposal_evidence_stale`**; the proposal can still be rejected. Only threats: a weight change
  or a protection may rest on old evidence.
- **Both reads** carry `evidence_dates: {newest, oldest, undated}` — ISO dates (UTC) or null, and the
  number of undated DOCUMENTS (a page with three signals is one) — over the active evidence, so the
  console can say "evidence from 12 Sep – 2 Oct · 3 undated".

The services API reads the key (approvability, decision) and so does the intel worker (prompt,
generation); `tests/test_ecs_task_defs.py` holds every container of an environment to one value.

## 4. Fix 2 — corroboration that ignores copies

ONE predicate still (`intel/corroboration.py`), for every caller: the approvable flag on both reads,
the decision's re-check, and a weight suggestion's per-option `corroborated`. It counts INDEPENDENT
source groups after two collapses (union-find over the active signals):

- **Echoes.** Two signals are one source when any of their verbatim excerpts, normalised (case,
  whitespace, punctuation, quote marks; apostrophes deleted), share a run of
  `ECHO_MIN_SHARED_RUN_WORDS` = **12** consecutive words, or at least `ECHO_MIN_TOKEN_OVERLAP` =
  **80%** of the shorter excerpt's distinct words appear in the other — the overlap rule only when
  the shorter has at least `ECHO_MIN_OVERLAP_WORDS` = **8** distinct words (two short phrases share
  most of their words by chance). Transitive. Twelve words is a sentence copied, not a topic shared:
  two reports of one announcement in their own words stay two sources.
- **A company's own domains** (`PUBLISHER_ORGANISATIONS`, `intel/bounds.py`) are one publisher:
  google (google.com, youtube.com, blog.youtube, youtu.be, googleblog.com), meta (meta.com, fb.com,
  facebook.com, instagram.com, threads.net, threads.com, whatsapp.com, messenger.com), x (x.com,
  twitter.com), bytedance (tiktok.com, bytedance.com), snap (snap.com, snapchat.com), microsoft
  (microsoft.com, linkedin.com, bing.com). `publisher_domain` is already the registrable domain.

A claim is corroborated by `CORROBORATION_MIN_PUBLISHERS` (2) such groups, or by a `listed` signal on
its own (unchanged). Both reads carry `independent_sources` (int), the count the rule compares.

### 4.1 What `listed` means (reported, not changed)

There is no publisher allow-list in code, config or a table. `trust = 'listed'` is set by the
pipeline on every document read from a REGISTERED source with a URL (`policy_page`, `feed` and its
items, `news`, `breach_index`, `regulator`, `research`) and on an operator-pasted `adhoc_url`; a page
a saved search (`search_query`) found is `web`. Registered sources come from `POST /sources` or are
registered by Suggest points stage 4 from model-proposed candidates an operator chose. On dev all 41
sources are origin `suggested`, and the `listed` publishers are ftc.gov, tiktok.com, snap.com,
eff.org, pimeyes.com, ic3.gov, haveibeenpwned.com, pewresearch.org, ncmec.org, stopncii.org,
gdpr-info.eu, sensity.ai, aarp.org, google.com. pimeyes.com is listed because
`https://pimeyes.com/en` was accepted as a `policy_page` source in a Suggest-points run. So "a listed
signal corroborates alone" means *any page from any accepted source* corroborates alone — including
a face-search engine's own marketing page, or a platform's own help page about itself. Left as is,
for the owner to decide.

## 5. Fix 3 — no duplicates, no double-counting

### 5.1 Same kind

- `duplicate_of` treats a tag set equal to OR inside the other's (one a subset of the other) as the
  same scope; partly overlapping or disjoint sets are still another scope.
- The prompt's pending event proposals are loaded over the tags of the new AND the related evidence,
  so a proposal an older run's signal already backs is shown and compared.
- The generation write re-checks, under `PROPOSAL_WRITE_LOCK` (`pg_advisory_xact_lock`), every
  pending proposal of the kind on the tags — not only those the run loaded — so a concurrent run's
  proposal is caught too. A repeat becomes an attachment of its new evidence
  (`proposal_converted_to_attach_at_write`) or, with none, is dropped
  (`proposal_dropped_duplicate_event_at_write`).

### 5.2 Cross kind (`intel/overlap.py`)

Two PENDING proposals **overlap** when they could be one fact (different kinds among
weight_change/threat_event/protection_event, or the same event kind; never two weight changes, never a
renewal), concern an overlapping tag (an event's `target.tags`; a weight change's option's mapped
tags in the live vocabulary), and rest on the same evidence: the evidence units they share are at
least `OVERLAP_MIN_SHARE` = **50%** of either one's units. A unit is a document (canonical URL hash)
joined with every document whose excerpts echo it. A company's other domains are NOT joined here: two
different announcements by one company are two pieces of evidence.

- Generation still writes such a proposal. Both reads carry `overlaps: [{proposal_id, kind}]` on a
  pending proposal (empty on any other), computed against up to `OVERLAP_POOL_MAX` (500) pending
  proposals.
- **Approving** one supersedes every pending proposal it overlaps — exactly what `overlaps` showed —
  in the decision's transaction, with **`supersede_reason = 'covered_by_decision'`** (migration
  **0048**), named on the decision's response (`superseded: [uuid]`) and in its audit row
  (`metadata.superseded`). **Rejecting** one changes nothing else.
- Every decision, every generation write and the reconcile take `PROPOSAL_WRITE_LOCK`, so two
  approvals that overlap each other serialise (the second answers `409 proposal_not_pending`) rather
  than deadlock.
- `propose-v4` also shows the model the pending weight changes and says: one body of evidence, one
  kind of proposal — a temporary incident is a threat_event, a lasting policy state a weight_change;
  never both, and never the same fact as both a protection and a risk.

## 6. Contract changes (services → backend)

| Change | Where |
|---|---|
| `evidence_dates: {newest: date \| null, oldest: date \| null, undated: int}` | both proposal reads |
| `independent_sources: int` | both proposal reads |
| `overlaps: [{proposal_id, kind}]` | both proposal reads (pending only) |
| `why_not: 'evidence_stale'` | both proposal reads |
| `409 proposal_evidence_stale` | `POST /v1/admin/intel/proposals/{id}/decision` |
| `superseded: [uuid]` | the decision's 200 body |
| `supersede_reason: 'covered_by_decision'` | the stored row on both reads |
| `published_at` (ISO or null) on `/v1/text` | the fetcher, read only by the intel worker |

No route is added or removed, and no `svc` view changes.

## 7. Deploy

Migration 0048, then services, services-worker and the fetcher (any order between those three: the
worker reads a missing `published_at` as null, and an older worker ignores the new key). The new
required key is in every dev and prod task definition, so a task definition from this commit must
ride with the image. On the way down: roll the code back, then run 0048's down (it relabels
`covered_by_decision` rows `newer_proposal`; the deciding approval's audit row still names them).
