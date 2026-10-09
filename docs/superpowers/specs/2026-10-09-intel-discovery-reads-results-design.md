# A saved search reads only pages its searches returned — design

Date: 2026-10-09. Owner: "yes if it is needed". Services only; no migration.

## 1. What dev showed

The first round of daily news watches (8e2a167) proposed three threats on its own (Facebook/Instagram,
Snapchat, TikTok). The X watch proposed nothing because it read no page at all:
`candidates: 6, fetch_unfetchable: 5, recently_read: 1`.

Its run log shows why. Its five searches returned exactly the reporting a threat would rest on: the Grok
lawsuits, a city's suit, research on X failing to remove non-consensual images. But the web-search tool
runs inside a code tool, and the model misread its results as empty ("Nothing came back, maybe empty
lists"). It spent its search budget and then answered "from general knowledge" with five remembered URLs,
none of them readable. The same misreading cut off an age source proposal on 2026-10-08.

## 2. The change

1. **In code** (`pipeline._discover`): a candidate URL that none of the call's searches returned (compared by
   canonical `url_hash`, so tracking parameters do not matter) is dropped and counted
   `candidate_not_in_search_results`. The dropped ones are replaced from the real results, in the order they
   came back, one page per site first and then the rest. The fill stops at the model's own candidate count,
   never fewer than `DISCOVERY_MIN_RESULT_PAGES` (5) and never more than `DISCOVERY_MAX_RESULT_PAGES` (8).
   Each added page counts `candidate_from_search_results`. A call whose searches returned nothing keeps the
   model's candidates, as before. Candidates the searches did return are read as the model chose them.
2. **In the prompt** (discover-v3): "Return only pages your searches returned, never a URL from memory."

This changes what is read, never what is concluded. The pages come from the AI's own searches, and
extraction and generation judge them as before (owner rule: no steering).
