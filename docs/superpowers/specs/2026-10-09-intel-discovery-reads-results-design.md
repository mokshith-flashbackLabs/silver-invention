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

## 3. Amended the same day: an empty answer over real results

The first rerun of the X watch under this change read nothing either. The model again saw its in-code
results as empty, and this time, obeying discover-v3, it returned NO candidates ("rather than fabricate
anything from memory") over 50 real results: an article on Ofcom's investigation into X's own AI-generated
deepfakes, a state attorney general's demand that X stop the flood, and others. The fill now also runs when
the model returns no candidates while its searches returned pages, reading up to
`DISCOVERY_MIN_RESULT_PAGES` (5) of them. A short list the model chose from real results is still read as
chosen, and a call with no results reads nothing.

## 4. Amended again: the model chooses from the real results

Filling in search-engine order read the first site of each search: for the X watch, a help page, a vendor's
product page, two January articles and a software listing, while the newer and more relevant reports sat
lower down. So when the read's own answer fails (a guess was dropped, or it answered with nothing although
its searches returned pages), one more call, with NO tool, shows the model those pages as plain text (url,
title, the search's own age) and it chooses at most the remaining slots (`choose_results`, choose-v1, same
model and effort as the read). Only pages the searches returned are kept (`choose_dropped_not_offered`
counts the rest). A choice of none is its verdict and nothing more is read. Search order
(`candidate_from_search_results`) is used only when that call gives no answer at all. `WebResult` now keeps
each result's title for this.
