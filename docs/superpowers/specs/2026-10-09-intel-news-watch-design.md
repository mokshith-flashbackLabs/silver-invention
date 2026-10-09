# A daily news watch for threats — design (specified, NOT built)

Date: 2026-10-09. Owner: "write spec for 1 but we are not going to do that today." Services only.

## 1. Problem

Threats need fresh news, and the pipeline does not go looking for it.

- **Stage-1 sources are evidence for points.** Since sources-v2 (2026-10-08) they are research, statistics,
  regulator reports and platform policies, at most two saved searches per option, read weekly
  (`DEFAULT_SOURCE_CHECK_EVERY_HOURS` 168).
- **Saved-search reads are not asked for recent news.** On dev after the rerun, 216 documents carried 222
  risk-raising incidents and only 29 were published in the last 90 days; 82 were older than a year.
- **The miss is concrete.** An independent search on 2026-10-09 found, for quiz platforms and within the
  window: Grok still generating sexual deepfakes of real people on X (Bureau of Investigative Journalism,
  2026-09-29), a class action over Grok deepfakes of children (2026-08-19), about 7,600 nudify ads through
  Meta's own Chinese ad partner (Tech Transparency Project via Bloomberg, 2026-07-27), and deepfake
  medical scam videos on Facebook that keep reappearing after removal (2026-10-06). None of these was in
  the documents dev held.

## 2. The design

### 2.1 Saved-search reads look for recent news
The discovery prompt (`discovery_request`, intel/prompts.py) gains, for a `search_query` source:
- prefer pages published within `threat_recency_days` of today, and put the current month and year in
  the query it runs;
- an older page is read only if it is a reference the evidence needs (a law's text, a platform policy),
  never as news.

`today` and `threat_recency_days` join the discovery payload, as they already appear in the generation
payload. No schema change.

### 2.2 One news watch per mapped platform tag
- **What:** for each registry tag of kind `platform` that a live quiz option maps (today: x, snapchat,
  instagram, facebook, tiktok, youtube), one `search_query` source with `origin = 'news_watch'`, a query
  such as "<platform label> deepfake OR impersonation OR leak OR sextortion", tags `[<slug>]`, and
  `check_every_hours = 24`.
- **Who creates it:** code, not the model. The vocabulary push (`PUT /v1/admin/intel/vocabulary`) already
  tells services which tags are mapped. On each push, services create a missing watch for a newly mapped
  platform tag and disable (never delete) the watch of a tag no longer mapped (`disabled_reason
  'unmapped'`, the existing pause). Idempotent: one watch per tag (unique on `origin, tags[1]` for
  `news_watch`).
- **Reads:** an ordinary `discovery` run on the existing schedule path, so generation (propose-v8 threats)
  runs on what it finds. The suggestion flow never registers or waits for a watch.
- **Operator control:** the watch shows on the Sources screen with its origin; an operator may disable it
  or change its query or cadence like any source. A disabled watch is not re-created by the next push.

### 2.3 Schema (one migration)
- `intel_sources.origin` admits `'news_watch'` (its CHECK gains the value).
- A partial unique index: one `news_watch` source per first tag.

### 2.4 Cost and limits
- Six watches a day, each a discovery run of up to `INTEL_MAX_WEB_SEARCHES_PER_RUN` (10) searches plus its
  page reads: at the observed USD 0.25–0.70 per run, about USD 1.50–4.20 a day on dev.
- The provider gate's daily budget (dev USD 50) and per-call estimate (USD 2.00, 0051) bound it; a run
  refused by the gate leaves the watch due, as today.
- `DISCOVERY_DEDUP_DAYS` (30) already stops a page being read twice in a month, so a daily watch does not
  re-read yesterday's results.

## 3. Tests owed
- A vocabulary push mapping a new platform tag creates exactly one watch, checked every 24 hours; a second
  push creates none; unmapping disables it; re-mapping a disabled watch leaves it disabled.
- The discovery request carries `today` and `threat_recency_days`, and the prompt asks for recent news.
- A watch's discovery run feeds generation, and a threat proposed from it cites its recent page.

## 4. Deploy order
Services only: the migration, then services and services-worker. No backend change; the console shows the
new origin as its raw value until its owner adds a word for it (prompt to hand over).
