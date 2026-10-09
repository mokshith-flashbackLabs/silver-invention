# An ongoing abuse is a threat while it is still reported — design

Date: 2026-10-09. Owner: after the full dev rerun proposed no threat at all, "2" (count an ongoing
abuse as a threat while it is still being reported) and "grok generates deepfakes in twitter, that is a
threat". Services only, prompt only: no migration, no code path changes.

## 1. What dev showed

After the 2026-10-08 wipe and rerun, 222 `incident`/`risk_up` signals produced 0 threat proposals.
Only 29 were published within `INTEL_THREAT_RECENCY_DAYS` (90). The model applied propose-v7 faithfully:
a threat was a TIME-LIMITED incident, current only if its evidence was recent, and "one the evidence
itself says has ended … is not a threat". Read that way:

- **Grok on X** began in late December 2025; X announced restrictions in January. Every report the run
  held was either older than 90 days or about January's events, so the model concluded it had ended.
  An independent search on 2026-10-09 found the Bureau of Investigative Journalism reporting on
  2026-09-29 that Grok "still allows users to generate, manipulate and edit sexualised deepfake
  images", and a California class action (2026-08-19) over Grok deepfakes of real children.
- **Nudify ads on Facebook and Instagram** have run since 2024; the Tech Transparency Project reported
  about 7,600 more through Meta's own Chinese ad partner on 2026-07-27, and 4,215 after Meta's new
  detection. v7 treated the pattern as old and lasting, so neither a threat nor (one kind per body of
  evidence) anything else.

Both are abuse that is still going on. Under v7 neither could ever be a threat again, because its start
date never moves.

## 2. The rule (propose-v8)

A threat is an INCIDENT or an ONGOING ABUSE that raises the risk to people exposed through a registry tag,
and it is current:
- an incident (a breach, a leak, a wave of deepfakes, an outage) when its evidence is recent, as before;
- an ongoing abuse (people actively misusing a platform or a tool on it: a built-in AI that keeps making
  sexual images of real people, nudify ads that keep running, impersonation or scam campaigns that keep
  coming back) for as long as the NEWEST report that it is still happening is within the window, however
  long ago it began. The model judges it by that report and cites it.

Also:
- A platform's claim to have stopped an abuse does not end it when a later report says it continues. An
  abuse the newest evidence says has ended is still not a threat.
- Lawsuits, rulings, studies and retrospectives are not incidents themselves, but one that reports the
  abuse is still happening is evidence that it is current.
- `expires_in_days` for an ongoing abuse: how long it plausibly continues without a fresh report (≤ 90,
  unchanged bound). When it expires and is still being reported, a new proposal can follow.
- One kind per body of evidence stays: an abuse still going on is a threat; a lasting change to a
  platform's own policy or default is a weight_change; a new safeguard is a protection.

The code needs no change: `evidence_stale` (intel/recency.py) drops a threat only when EVERY cited signal
is dated and older than the window, so one recent report keeps an ongoing abuse current.

## 3. What this does not fix

The rule can only use evidence the pipeline has read. The 2026-09-29 Grok report was not among the
documents dev held; the stage-1 sources are evidence for points, checked weekly. Getting fresh news in is
`2026-10-09-intel-news-watch-design.md` (specified, not built). Until then an operator can paste a report
as a document (`POST /v1/admin/intel/documents`), and its generation runs under this rule.
