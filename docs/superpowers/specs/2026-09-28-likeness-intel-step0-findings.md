# Likeness intel — step 0 findings (2026-09-28)

Spec: `docs/superpowers/specs/2026-09-27-likeness-intel-design.md` §8 step 0. This is a spike;
its output is the answers recorded here, not code kept for its own sake — see
`devtools/intel_access_probe.py`'s docstring for when that file is deleted.

**Controller ruling for this task run (2026-09-28): Step 3 (the live probe against Claude
Platform on AWS) was explicitly NOT executed.** No call was made to Claude Platform on AWS,
Anthropic, or any AWS API. The dev/prod workspace ids are not available in anything readable in
this session, the prod account is off-limits to this session, and the calls cost money — the
owner runs `devtools/intel_access_probe.py` later, against both workspaces, and fills in the
`PENDING` rows below from its output.

## Fact table

| Fact | Dev | Prod |
|---|---|---|
| `INTEL_ANTHROPIC_REGION` | `ap-south-1` — **not yet confirmed by the probe**; per AWS docs, Claude Platform on AWS workspaces bind to "All AWS commercial regions" (see Region availability below), so `ap-south-1` is an eligible binding, but only the probe confirms it actually serves `claude-sonnet-5` and `claude-opus-5-5` | `us-east-1` — same caveat; `us-east-1` is an eligible binding per docs, unconfirmed by probe |
| `ANTHROPIC_AWS_WORKSPACE_ID` | `PENDING — owner runs devtools/intel_access_probe.py` | `PENDING — owner runs devtools/intel_access_probe.py` |
| `claude-sonnet-5` served | `PENDING — owner runs devtools/intel_access_probe.py` | `PENDING — owner runs devtools/intel_access_probe.py` |
| `claude-opus-5-5` served | `PENDING — owner runs devtools/intel_access_probe.py` | `PENDING — owner runs devtools/intel_access_probe.py` |
| web search works | `PENDING — owner runs devtools/intel_access_probe.py` | `PENDING — owner runs devtools/intel_access_probe.py` |

**Stop condition from the brief still applies and is unresolved by this task**: "Stop here if no
region serves both models, and tell the owner" — that determination requires Step 3, which this
task did not run. Do not treat `ap-south-1` / `us-east-1` as verified until the probe output says
so.

## Step 1 — installed SDK, into the shared services venv

Installed with:
```
"/c/Users/Mokshith work/Project/imageShield/image_flashbacklabs/.venv/Scripts/python.exe" -m pip install "anthropic[aws]" publicsuffixlist
```

- `anthropic` version installed: **1.8.0** (brought in `httpx2==2.13.1` as a second HTTP stack,
  as the design spec §1 anticipated — `httpx` stays for the rest of the codebase).
- `publicsuffixlist` version installed: **1.0.2.20260925**.
- `anthropic.AnthropicAWS` **exists**. `inspect.signature(anthropic.AnthropicAWS.__init__)`
  printed verbatim:
  ```
  (self, *, api_key: 'str | None' = None, aws_access_key: 'str | None' = None,
   aws_secret_key: 'str | None' = None, aws_region: 'str | None' = None,
   aws_profile: 'str | None' = None, aws_session_token: 'str | None' = None,
   workspace_id: 'str | None' = None, skip_auth: 'bool' = False,
   base_url: 'str | httpx2.URL | None' = None,
   timeout: 'float | httpx2.Timeout | None | NotGiven' = NOT_GIVEN,
   max_retries: 'int' = 2, default_headers: 'Mapping[str, str] | None' = None,
   default_query: 'Mapping[str, object] | None' = None,
   http_client: 'httpx2.Client | None' = None,
   middleware: 'Sequence[MiddlewareInput] | None' = None,
   _strict_response_validation: 'bool' = False, auth_token: 'str | None' = None,
   webhook_key: 'str | None' = None) -> 'None'
  ```
  The region and workspace parameter names are **`aws_region`** and **`workspace_id`** — exactly
  what the brief's probe script already assumes, so `devtools/intel_access_probe.py` needed no
  edit on this point. `max_retries` defaults to `2`.
- `anthropic.AsyncAnthropicAWS` **exists**, with the identical parameter set (only
  `http_client: httpx2.AsyncClient | None` differs).
- `client.messages.parse` **exists** on `AnthropicAWS` (verified via
  `hasattr(c.messages, 'parse')` on a `skip_auth=True` instance), alongside `client.messages.create`.

No credentials were used or required for any of Step 1 — `skip_auth=True` lets the client
construct without touching AWS, which is how the constructor signature and the `messages.parse`
existence were confirmed without making a network call.

## Step 2 — the probe

`devtools/intel_access_probe.py` is written per the brief, unmodified in its constructor call
(`aws_region=region, workspace_id=workspace` already matches Step 1's findings). Its docstring
now says explicitly that it stays committed until the owner has run it and this findings file's
`PENDING` rows are filled in — see the file for the exact wording.

## Step 3 — NOT RUN (controller ruling)

No probe execution occurred in this session. This is the one row in the interfaces list that
only the owner, with real credentials and real workspace ids, can produce.

## Step 4 — IAM actions, ARN shape, and prices (official sources, fetched and quoted)

**IAM action for the Messages API**: `CreateInference`, which authorizes `POST /v1/messages`
(`CountTokens` separately authorizes `POST /v1/messages/count_tokens` — token counting, not
inference). Confirmed identically on two official sources:
- AWS Service Authorization Reference, service prefix `aws-external-anthropic`:
  https://docs.aws.amazon.com/service-authorization/latest/reference/list_aws-external-anthropic.html
  (lists `CreateInference` as one of the ~65-71 actions; full route mapping not on this page)
- Anthropic's own IAM action reference (this is the page that actually spells out the
  route-to-action table):
  https://platform.claude.com/docs/en/api/claude-platform-on-aws-iam-actions
  — quoting its "Inference" table verbatim:
  | Action | Routes authorized |
  |---|---|
  | `CreateInference` | `POST /v1/messages` |
  | `CountTokens` | `POST /v1/messages/count_tokens` |

  The narrowest managed policy for running inference is `AnthropicInferenceAccess`, which grants
  `CreateInference`, `CreateBatchInference`, `CancelBatchInference`, `DeleteBatchInference`,
  `CountTokens`, plus `Get*`/`List*` and `CallWithBearerToken` (same source). Note the same page's
  warning: `CreateInference` and `CreateBatchInference` are separate actions — denying one does
  not block the other.

**Resource ARN format for a workspace**, quoted verbatim, identical on both sources:
```
arn:aws:aws-external-anthropic:{region}:{account-id}:workspace/{workspace-id}
```
(AWS Service Authorization Reference spells the placeholders `${Partition}`/`${Region}`/
`${Account}`/`${ResourceId}`; Anthropic's own page spells them `{region}`/`{account-id}`/
`{workspace-id}` — same shape, quoted from:
https://platform.claude.com/docs/en/api/claude-platform-on-aws-iam-actions#service-details)

**Region availability, per official docs** (this is a documentation claim, not a probed fact —
see the Stop condition note above): the Claude Platform on AWS build-with-claude guide states
region is required with no default and: *"All AWS commercial regions are supported"* for the
Messages-API-style client (`aws_region`/`AWS_REGION`), and its Bedrock-migration comparison table
repeats, for "Region availability": *"All AWS commercial regions"* — source:
https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws
(sections "Region resolution" and "Migrating from Amazon Bedrock"). Both `ap-south-1` and
`us-east-1` are AWS commercial regions, so both are eligible workspace-binding regions per this
doc. This says nothing about whether a specific model like `claude-opus-5-5` actually answers in
a given region's endpoint at request time — that is exactly what the probe (Step 3, not run) is
for. No official page enumerating per-model, per-region availability was found; see below.

**NOT FOUND — a per-model, per-region availability matrix for Claude Platform on AWS** (i.e. a
page saying "claude-opus-5-5 is served from ap-south-1" specifically). Searched: the build-with-claude
page's own text (region section quoted above only states region *binding* rules, not per-model
serving), `https://docs.aws.amazon.com/claude-platform/latest/userguide/feature-support.html`
(lists feature parity, not per-region model availability), and general web search for
"Claude Platform on AWS" + region + model list. If such a matrix exists it was not surfaced by an
official source in this session; the probe is the authoritative check.

**Prices per million tokens**, quoted verbatim from the official Anthropic pricing page
(https://platform.claude.com/docs/en/about-claude/pricing, "Model pricing" table). The page states
Claude Platform on AWS bills "at standard per-model, per-feature rates (same as Claude API
pricing)" via Claude Consumption Units at $0.01/CCU, so this table is the correct source for
per-token rates on that platform too:

| Model (docs display name) | Base input | 5m cache write | 1h cache write | Cache read (hit) | Output |
|---|---|---|---|---|---|
| Claude Sonnet 5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok | $20 / MTok |

**Model-name mapping assumption, stated explicitly**: the pricing page lists models by display
name ("Claude Sonnet 5", "Claude Opus 5.5"), not by the API model-id strings this spec and the
probe use (`claude-sonnet-5`, `claude-opus-5-5`). The mapping used above (Sonnet 5 ↔
`claude-sonnet-5`, Opus 5.5 ↔ `claude-opus-5-5`) follows Anthropic's normal id-naming convention
but was not independently confirmed against a page that prints the literal id strings. Treat this
row as `ASSUMED, not literally sourced` if a mismatch ever shows up in the probe's `answered_by`
field.

A footnote on the pricing table (verbatim): *"The $2/$10 per million input/output token pricing
for Claude Sonnet 5, announced at launch as introductory pricing through August 31, 2026, is now
the standard price. The previously scheduled increase to $3/$15 per million input/output tokens
on September 1, 2026 will not occur."* — i.e. $2/$10 is confirmed as the durable Sonnet 5 rate,
not a lapsing promo.

**Web search price**: **$10 per 1,000 searches**, plus standard token costs for search-generated
content, quoted verbatim from the same pricing page's "Web search tool" section: *"Web search is
available on the Claude API for $10 per 1,000 searches, plus standard token costs for
search-generated content."* Each search counts as one use regardless of result count; a search
that errors is not billed.

Incidentally, the pricing page's "Code execution tool" section names the literal tool-version
string `web_search_20260209` (*"When web_search_20260209 (or later) ... is included in your API
request, there are no additional charges for code execution tool calls..."*), which is the same
tool-version literal the brief's probe script takes as `--web-search-type`. That is not proof the
string is still current on the day the owner runs the probe, but it is official confirmation the
string was a real, current tool version as of this fetch (2026-09-28).

**No spend-limit / budget control exists on Claude Platform on AWS itself**, per
`feature-support.html`'s "Features not currently available" list, quoted verbatim: *"Spend
limits: Not available. Rely on AWS billing controls instead."* This matters for the daily-budget
row below: `claude_intel`'s cap has to be enforced in our own provider-gate budget code
(CLAUDE.md services §7.6 / INVARIANTS #38), not by anything Anthropic or AWS will refuse at the
gateway.

## Step 5 — worst-case per-call estimate

Formula (as given):
```
cost_per_call_usd = (max_input_tokens × input_rate + max_output_tokens × output_rate) / 1_000_000
                    + INTEL_MAX_WEB_SEARCHES_PER_RUN × (price_per_1000_searches / 1000)
```
with `max_input_tokens = 60000`, `max_output_tokens = 8000`, `INTEL_MAX_WEB_SEARCHES_PER_RUN = 5`.

Computed per model (both use the same `$10/1000` web-search term = `5 × $0.01 = $0.05`):

- `claude-sonnet-5` (`INTEL_EXTRACTION_MODEL`): `(60000×2 + 8000×10)/1,000,000 + 0.05`
  `= (120,000 + 80,000)/1,000,000 + 0.05 = 0.20 + 0.05 = $0.25`
- `claude-opus-5-5` (`INTEL_PROPOSAL_MODEL`): `(60000×4 + 8000×20)/1,000,000 + 0.05`
  `= (240,000 + 160,000)/1,000,000 + 0.05 = 0.40 + 0.05 = $0.45`

**Worst case across both models: `$0.45` per call** (Opus 5.5, the more expensive of the two, and
the one named `INTEL_PROPOSAL_MODEL` in the design spec §9's config table — the proposal step is
the one most likely to run with the web-search tool attached). This ignores prompt-caching
discounts (cache reads are cheaper, not more expensive, so omitting them keeps this a ceiling) and
ignores the `inference_geo: "us"` 1.1x multiplier, which is opt-in per request and not the
default (`inference_geo: "global"` is the default per the pricing page's "Data residency pricing"
section) — so it is not included in a "worst case" that assumes default behavior. If a future
change pins `inference_geo` to `"us"` for either region, multiply this figure by 1.1.

## `claude_intel` daily budget

**`PENDING — owner decision`.** Per this task's controller ruling, nobody was asked for this
figure in this session; it is a finance decision reserved for the owner. See "Owner actions"
below.

## Step 6 — recorded findings block (brief's requested format)

```markdown
| Fact | Dev | Prod |
|---|---|---|
| INTEL_ANTHROPIC_REGION | ap-south-1 (unconfirmed by probe; AWS docs say all commercial regions are supported for workspace binding — see Step 4) | us-east-1 (same caveat) |
| ANTHROPIC_AWS_WORKSPACE_ID | PENDING — owner runs devtools/intel_access_probe.py | PENDING — owner runs devtools/intel_access_probe.py |
| claude-sonnet-5 served | PENDING | PENDING |
| claude-opus-5-5 served | PENDING | PENDING |
| web search works | PENDING | PENDING |

- AnthropicAWS constructor parameters: `aws_region`, `workspace_id`
- anthropic[aws] pin: `anthropic[aws]==1.8.0`; publicsuffixlist pin: `publicsuffixlist==1.0.2.20260925`
- IAM actions: `CreateInference` (POST /v1/messages), `CountTokens` (POST /v1/messages/count_tokens); resource ARN shape: `arn:aws:aws-external-anthropic:{region}:{account-id}:workspace/{workspace-id}`
- Prices per MTok (in/out/cache-write-5m/cache-write-1h/cache-read): sonnet-5 $2/$10/$2.50/$4/$0.20; opus-5-5 $4/$20/$5/$8/$0.20; web search $10/1000 searches
- claude_intel cost_per_call_usd (worst case): 0.45 (opus-5-5); sonnet-5-only estimate: 0.25
- claude_intel daily_budget_usd (owner, 2026-09-28): PENDING — owner decision
```

## Owner actions

Exactly what the owner needs to do to close out the `PENDING` rows above:

1. **Provide the dev workspace id** (`ANTHROPIC_AWS_WORKSPACE_ID` for `ap-south-1`) and the
   **prod workspace id** (for `us-east-1`).
2. **Run the probe for dev**, with operator AWS credentials for the dev account in the
   environment:
   ```bash
   AWS_PROFILE=<dev-profile> python devtools/intel_access_probe.py --region ap-south-1 --workspace <dev-workspace-id> --web-search-type web_search_20260209
   ```
3. **Run the probe for prod**, with operator AWS credentials for the prod account in the
   environment:
   ```bash
   AWS_PROFILE=<prod-profile> python devtools/intel_access_probe.py --region us-east-1 --workspace <prod-workspace-id> --web-search-type web_search_20260209
   ```
   Expected per the brief: each model line's `answered_by` equals the requested model, and the
   web-search line's `usage.server_tool_use.web_search_requests >= 1`. If `ap-south-1` fails,
   retry dev with the nearest served region and record which region it is, per the brief's Step 3
   note — and if no region serves both models, stop and say so rather than proceeding.
4. **Decide and provide the `claude_intel` daily budget in USD.** This is a finance decision this
   task deliberately did not ask for; record the answer verbatim once given (see INVARIANTS #38's
   fail-closed rule — a budget with an unknown/unset cap must not silently allow unbounded spend).
5. Once 1–4 are done, fill in every `PENDING` cell above from the probe's actual output, then
   follow the brief's Step 7 (delete `devtools/intel_access_probe.py` and commit only the
   completed findings file).
