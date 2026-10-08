# Source proposal v2 — design

Date: 2026-10-08. Owner: "critic it", then "fix it", "maybe also increase the search limits".
Services only.

## 1. What dev showed (stage 1, sources-v1, after the full intel wipe)

- **gender: no sources, no search, "completed".** Twice the model reasoned "this is just a demographic
  question … not tied to any platform's likeness handling" and returned empty lists in 8 s. v1 asked only
  for sources reporting "how that platform, service or practice treats people's photos".
- **age: cut off and lost.** Ten searches spent in one batch (the web-search tool runs inside code),
  turns spent retrying an exhausted cap, a long plan, then `max_tokens` at 8 000 (thinking included):
  `source_proposal_max_tokens`, $0.39, nothing kept. The retry fit only just.
- **The rest:** prior_misuse listed the same five help services (StopNCII, Take It Down,
  HaveIBeenPwned, TinEye) under every option; posting_volume leaned on generic platform policies and a
  2013 survey; the monitoring question was 14 saved searches out of 15. Only platforms was good.

## 2. The change

1. **sources-v2 prompt.** Sources are EVIDENCE of how an answer changes a person's chance of likeness
   misuse. A platform option takes its own policies and reporting; an option about the person (age,
   gender, habits, history, monitoring) takes research, statistics, regulator and law-enforcement
   reports and serious journalism, and the prompt says such evidence exists for every question about
   the person. Search before answering, one query at a time; every URL from a search; prefer the last
   two years and nothing over five; a source repeated across options must say what it shows for each;
   at most two saved searches per option; never a help/removal/reporting service, a product page or
   abusive content.
2. **At most two saved searches per option, in code** (`MAX_PROPOSED_SEARCHES_PER_OPTION`): extras are
   dropped and counted `candidate_dropped_search_over_cap`.
3. **Nothing for any answer fails.** When no option has a proposed or an existing source, the run ends
   `failed`, `no_sources_found` (`source_proposal_empty`). Existing sources alone still complete it.
4. **Room to answer.** The source proposal call gets its own `max_tokens` of 16 000 (every other call
   keeps 8 000); its effort stays the model's default.
5. **More searches.** `INTEL_MAX_SOURCE_PROPOSAL_SEARCHES` 10 → 25 (dev and the prod template).
6. **The gate's estimate follows** (migration 0051): `claude_intel.cost_per_call_usd` 0.45 → 2.00.
   Real stage-1 calls already reached 0.71 at 10 searches; at 25 searches and 16k output the worst
   case is about 1.71. Down restores 0.45.

## 3. Not in this change

- Checking in code that each URL came from a search (stage 3's validation still fetches every one).
- The search limit of saved-search reads (`INTEL_MAX_WEB_SEARCHES_PER_RUN`) is unchanged.
