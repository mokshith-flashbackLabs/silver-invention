# Suggest points also drafts the score page's words — design

Date: 2026-10-11. Owner request, showing the funnel's result screen (quiz.imageshield.com): the
cards "The primary risk factors that determined your initial score" and "General recommendations"
are hardcoded in the web app, and "these should be driven by the AI research we did, not hardcoded
things. These two sections should be written by our AI."

This is the services half of two coordinated changes. The backend half,
`image_backend/docs/superpowers/specs/2026-10-11-researched-score-insights-design.md`, stores the
words an operator applies, checks them again, and serves them on the score response. This half
only makes the AI **draft** them. It adds no proposal kind, no run kind, no table, no migration
and no `svc` view.

## 1. What the owner decided

- **The AI writes the words; a person applies them.** As with a threat's body or a weight change's
  reason, the model proposes and an operator decides.
- **Risk factors are small, and only the major contributors get one.** "risk factors should be
  small and not all is needed, AI writes them [for] the major contributors that are affecting the
  likeness score." An answer that costs no points is not a risk factor and gets no line.
- **General recommendations are the same for everyone** (the owner chose "same for everyone" over
  per-answer steps and over live findings). A short list, identical for every visitor.
- **No new dimension.** Asked whether this adds a fifth kind beside weight changes, threats,
  protections and coverage gaps: it does not. The words are fields on the Suggest points result the
  AI already writes per question. The owner's reminder: "these are just read-only for the user in
  the web app". They move no score and need no lifecycle.

## 2. What the AI drafts (`suggest-v2` → `suggest-v3`)

`SuggestedOptionWeight` (`intel/schemas.py`) gains two fields, and `SuggestionOutput` gains one.
As with every `_Out` model, no bounds are set in the schema; code bounds them (§4).

| Field | Where | What it is |
|---|---|---|
| `factor_title` | each option | A short name for the risk this answer carries, of two to four words. At most 40 characters. |
| `factor_line` | each option | One or two short sentences of at most 160 characters: why people in this group, or with this practice, face more likeness misuse, taken from the cited evidence. |
| `recommendation` | the question, at most one | `{title, body, link_url, link_label, signal_ids}`: a step any adult can take to lower the risk this question measures. `title` is an instruction of at most 80 characters, and `body` is one sentence of at most 200. |

Both factor fields are `""` for an option that gets none. `recommendation` is null when the
evidence names no step.

### 2.1 The prompt rules

These rules are generic. They name no platform, product or case we have seen (owner rule
2026-10-09: the AI must find things unaided, and a prompt example that mirrors a known case
steers it).

- **Write `factor_title` and `factor_line` only for an option whose deduction is above 0 and that
  cites evidence.** Otherwise leave both empty.
- `factor_line` describes the risk of the group or practice. It never refers to the reader's
  answer ("you answered", "your answer"), because it is shown to everyone who chose that answer.
- **Figures.** Write any figure in digits. Copy it exactly as it appears in a `quotes` entry of
  evidence this item cites, with its unit, and never round, combine or derive one. If no quote
  carries a figure, write the line without one.
- **Never** say anyone is safe, protected or in the clear. Never say that anything of the reader's
  was found, detected or matched. Name no private individual. Give no contact details, and put no
  link or web address in any text field.
- `recommendation` is written only when cited evidence names a concrete step that **any adult**
  can take, whatever they answered. It is never a step for one group only, and never "contact
  ImageShield". `link_url` is the `url` of one evidence item it cites, or null. `signal_ids` names
  the evidence behind it.
- Every evidence item in the request gains **`quotes`**: its verbatim excerpts (`ContextSignal.excerpts`,
  which corroboration already loads). They are the only text a figure may be copied from.
  Treat them as untrusted data, as the prompt already says of summaries.

`SUGGEST_PROMPT_VERSION = "suggest-v3"`, pinned by the existing test.

## 3. Unchanged

- Deductions, rationale, tags, `research_searches` and research rounds behave exactly as today.
- Nothing about a person crosses. The request is still the draft question, its evidence and the
  tag registry, never a person and never an answer (INVARIANTS #48). The lines describe the
  quiz's own options.
- Threat and protection proposals are untouched. Their `body` ("what it means for you") already
  exists, and the backend shows it on the same card.

## 4. Code checks (`validate_suggestion`)

Each check is counted into `counts`, as every drop already is. Text is never truncated.

**The factor pair** (`factor_title` and `factor_line`) is kept or dropped together. The option
itself is always kept.

| Drop | When |
|---|---|
| `factor_line_no_deduction` | The option's deduction is null or 0 after `_deduction`, including when it lost its evidence. |
| `factor_line_missing` | Either field is empty after masking and normalising. |
| `factor_line_too_long` | `factor_title` > 40 or `factor_line` > 160 characters. |
| `factor_line_forbidden_words` | Either field matches the word list (§4.1). |
| `factor_line_figure_not_quoted` | A figure in either field appears in no excerpt of a signal the option cites (§4.2). |

**The recommendation** is dropped whole for out-of-bounds text, the word list, an unquoted figure,
or no surviving cited signal (`recommendation_dropped_*`). Its link follows the threat-action rule
(`generation.py` `_threat_action`). The link is kept only when it is `https://`, at most 500
characters, and its `url_hash` is a cited signal's `document_key`. Otherwise the link and its label
are dropped (`recommendation_link_not_cited`) and the recommendation is kept. `link_label` (at most
60 characters) needs a link.

### 4.1 The word list

This is a new module, `intel/user_copy.py`, matched case-insensitively. The backend applies the
same list again when an operator applies the words; the two lists must match, and a test in each
repo pins its own copy.

- reassurance: `safe`, `all-clear` / `all clear`, `guarantee(d|s)`
- a finding: `found`, `detected`, `match` / `matches` / `matched`, `hit` / `hits`, `your photos`
- the reader's answer: `you answered`, `your answer`
- a spelled-out figure: `percent`, `per cent` (figures are digits, so the check in §4.2 can see them)
- an address: `http`, `www.`, any `name.tld`, an email or phone shape (the existing `pii` masks
  run first, so a masked token also fails)

### 4.2 The figure check

A figure is a run of digits, optionally with `,` or `.` between digits, and with an optional
trailing `%`. Each figure must occur in at least one excerpt (`quote_text`) of a signal the item
cites. Thousands separators are removed on both sides before comparing. A text with no digits
passes. The backend runs the same check again on the words an operator applies, edited or not.
Running it here as well means the console never shows drafted words the backend would refuse.

## 5. Storage and reads

- **No migration.** `intel_proposals.target` is JSONB.
  - Each `target.options[i]` gains `factor_title` and `factor_line` (`""` when none). `OptionSuggestion.as_json`
    and `from_json` carry them, with defaults, so pre-v3 rows still parse.
  - `target.recommendation` holds `{title, body, link_url, link_label, signal_ids}`, or is absent.
- The recommendation's cited signals join the proposal's `intel_proposal_signals`, as the
  options' already do.
- **Research rounds.** A kept option keeps the words of the pass it was kept from
  (`keep_backed`). The recommendation is the last valid one across the passes.
- **Reads.** `GET /v1/admin/intel/proposals/{id}` and the poll `GET /v1/admin/intel/weight-suggestions/{run_id}`
  serve the new fields on each `options[]` entry. They also serve a top-level `recommendation`:
  `{title, body, link_url, link_label, signal_ids, corroborated, why_not}` or null. `corroborated`
  and `why_not` are computed at read time with the same rule options use (two independent
  publishers unless a cited signal is `listed`).
- `PROXY_INTEGRATION.md` S7 (`SuggestionOption`) is amended with the fields.

## 6. Tests

- The prompt is `suggest-v3`, asks for both factor fields and the recommendation, shows `quotes`,
  and names no platform, product or case (the propose-v9 no-steering test pattern).
- A factor pair survives only beside a deduction above 0 with surviving evidence. Each drop reason
  in §4 counts once and keeps the option, with its deduction and rationale unchanged.
- A figure present in a cited excerpt passes, including `1,200` against `1200` and a trailing `%`.
  A figure present only in a signal's summary, or only in an uncited signal's excerpt, drops the pair.
- Every word-list category drops the item.
- A recommendation link to an uncited page loses the link and keeps the recommendation. A
  recommendation with no surviving cited signal is dropped.
- A pre-v3 proposal row parses and reads with empty factor fields and `recommendation: null`.
- Across research rounds, a kept option's words come from its kept pass.

## 7. Deploy and cost

- **Deploy services first.** The backend's readers are passthrough, so an older backend ignores
  the fields, and the backend's apply routes need them to exist. No migration and no task-def key.
- Rollback is the previous image. A v3 proposal's extra fields are ignored by v2 code.
- Cost: each request grows by the evidence quotes, which are up to 600 characters each and a few
  per signal. Each answer gains up to 200 output characters. Suggest points runs on an operator's
  press, so the cost scales with presses, not with users.
- **Prod:** the `claude_intel` provider is still off on prod, so nothing is drafted there until it
  is enabled. That is the operator's switch, not part of this change.
