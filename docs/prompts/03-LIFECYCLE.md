# Out-of-band task — infringement lifecycle: feedback and recheck

**Not a numbered step.** Two small additions that complete the monitoring product and unblock two
things the proxy's phase 9 already built. Run on `main`.

---

## Why both, together

They are the two halves of an infringement's life after it is found. The proxy has a report surface
with no way to record what a user said about a hit, and a scoring model whose `live_exposure_count`
has no signal because nothing ever marks a URL dead.

---

## Part 1 — user feedback on a hit

`SCHEMA.md` already specifies `hit_feedback`. Build it against `infringements`.

### Migration

Number it to follow whatever is currently highest in `migrations/`.

```sql
CREATE TABLE infringement_feedback (
  feedback_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id  UUID NOT NULL REFERENCES infringements(infringement_id)
                     ON DELETE CASCADE,
  user_ref         UUID NOT NULL,
  signal           TEXT NOT NULL CHECK (signal IN ('not_me','confirmed','uncertain')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX infringement_feedback_infr_idx
  ON infringement_feedback (infringement_id, created_at DESC);
```

Append-only. A user changing their mind writes a second row; the history is the record.

### Endpoint

```
POST /v1/infringements/{infringement_id}/feedback
  { user_ref, signal }
  -> 200 { status }

  404 if the infringement does not exist OR belongs to a different user_ref.
      Do not distinguish the two — that difference is an enumeration oracle.

  signal -> infringements.status:
    not_me     -> 'dismissed_not_me'
    confirmed  -> 'acknowledged'
    uncertain  -> unchanged, feedback recorded
```

### The rule that matters

**`not_me` never adjusts the user's identity vectors, never suppresses future matches from that
domain, and never feeds banding.** It writes a feedback row for reviewer calibration and nothing else.

The reason is specific: users reject true positives under distress, and it is common. If rejections
retrained the identity index, the users most affected by real abuse would systematically degrade
their own protection — the failure would be invisible and would concentrate on exactly the people the
product exists for.

Keep the signal. Do not act on it automatically.

---

## Part 2 — the recheck loop

`infringements.url_alive` exists and nothing ever sets it false. Every infringement is permanently
"live", so any count of live exposure equals the total.

A dead URL is also the only good news v1 can deliver. Detection without takedown is otherwise
alerts-with-no-remedy, and *"this came down"* is the one thing the system can tell someone that is
unambiguously positive.

### Behaviour

```
Weekly, per infringement with url_alive = true:
  HEAD the page_url
    2xx / 3xx      -> alive. Set last_checked_at only.
    404 / 410      -> url_alive = false, last_checked_at
    403 / 401      -> alive. Gated, not gone.
    timeout / DNS  -> UNCHANGED. Not evidence of removal.
    5xx            -> UNCHANGED. Server trouble, not removal.

NEVER delete an infringement. A dead URL is still evidence, and the user has
already been told about it.
```

**Only 404 and 410 mark it dead.** Anything else leaves the row alone. Marking a hit resolved because
a site was briefly down would tell a victim their problem is fixed when it is not — the asymmetry
runs the same direction as everywhere else in this system.

### Network posture

This fetches hostile domains. Same guards as the crop fetcher in `ARCHITECTURE.md` §3.6:

```
- HEAD only. Never GET. We need liveness, not bytes.
- SSRF guards applied AFTER DNS resolution, rejecting private ranges
- 5s timeout, 2 redirects max, no body read
- Domain allowlist sourced from content_urls.source_domain
- Runs as its own worker, not in the API process
```

`RECHECK_INTERVAL_DAYS` and `RECHECK_BATCH_SIZE` from config. Rate-limit per domain — probing one
site's 400 URLs in a burst gets you blocked and looks like an attack.

### Reporting

`GET /v1/search/infringements` already returns `status`. Add `url_alive` and `last_checked_at` to
`InfringementItem` so the proxy can render "no longer online" and compute live exposure without a
second call.

---

## Also in this commit — drop `image_url` from the list response

`InfringementItem.image_url` hands a direct link to the infringing image to any caller of the list
endpoint.

Rendering a report list needs domain, dates, band, status. It does not need the image URL. That is
only needed to render a crop, which is a separate call with its own gate — and the crop fetcher is
not built.

Remove the field from `InfringementItem`. **Keep the column**; it is evidence. This closes a
contradiction the proxy team raised: no image bytes and no direct image link reach a user-facing
read.

This is a breaking change to a contract the proxy consumes. Flag it in your report.

---

## Done when

- `POST /v1/infringements/{id}/feedback` with a mismatched `user_ref` returns **404**, identical to a
  non-existent id — assert both produce byte-identical responses
- `not_me` writes a feedback row, sets status, and provably does **not** touch `enrolments`,
  `attestations`, or any band — assert with row checksums before and after
- a second, contradictory feedback writes a second row; the first is not modified
- a 404 marks `url_alive = false`; a timeout, a 500, and a 403 each leave it **unchanged** — four
  separate tests
- the recheck worker never issues a GET — assert on the mocked transport
- recheck refuses `169.254.169.254` and any domain absent from `content_urls`
- per-domain rate limiting is applied; 400 URLs on one domain do not burst
- `InfringementItem` no longer carries `image_url`, and the column still exists
- `GET /v1/search/infringements` returns `url_alive` and `last_checked_at`

Stop when done.

---

## Standing rules

```
- Cite file:line when describing existing behaviour. Mark anything not read
  directly as INFERRED.
- If anything here conflicts with CLAUDE.md §4 (invariants), STOP AND ASK.
- Doc corrections land in the same commit as the code they describe.
- When the task is done, STOP. Report before starting the next one.
```
