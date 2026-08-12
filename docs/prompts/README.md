# Queued tasks

Five tasks, numbered in execution order. Each is self-contained — scope, contract changes, and
done-when criteria are in the file. Run on `main`.

| # | File | What | Why in this position |
|---|---|---|---|
| 01 | `01-CONSENT-REF.md` | `enrolments.consent_ref` NOT NULL. Consent lives in the proxy; we hold a reference. | Closes the invariant #2 gap — nothing currently prevents a consent-free enrolment |
| 02 | `02-SEED-URL.md` | Seeds store a presigned URL, which expires — every seed dies ~a week after creation. Separates durable ref from per-run credential. | Real bug. Shares endpoints with 01, so same session |
| 03 | `03-LIFECYCLE.md` | Feedback on a hit, plus the recheck loop that sets `url_alive`. Drops `image_url` from the list response. | Two of the proxy's five ports |
| 04 | `04-STEP-9-INFRA.md` | IaC, IAM with no `s3:*`, blocking CI gates, `OPERATIONS.md` | Gates on the rest existing |
| 05 | `05-ATTRIBUTION.md` | Detect faces in an uploaded photo, match each against enrolled people, register one seed per matched person. | The critical path for monitoring to find anything |

## Why 05 matters most, despite being last

Hive is image search: it finds *the image*, reposted or altered. The enrolment `ReferenceImage` is a
selfie taken thirty seconds earlier that nobody has ever reposted, so searching it finds nothing —
correctly, forever.

The seeds that matter are the social-media photos screen 16 asks for. Attribution is what tells us
which enrolled person a photo should be a seed *for*. Without it, monitoring runs perfectly and
reports nothing.

## Rules for all five

- **Stop at the end of each task.** Report before starting the next.
- **If anything conflicts with `CLAUDE.md` §4, stop and ask** rather than resolving it. That rule has
  already caught four real contradictions in these documents.
- **Doc corrections land in the same commit as the code they describe.** 01, 03 and 05 all carry doc
  edits that must not be deferred.

## Three things that will look wrong

All deliberate. Flagged so they are not "fixed".

**02 inverts a validator.** `source_object_ref` must now *reject* `https://`, where the current
`SeedCreateRequest` requires it. A presigned URL arriving at seed creation is the exact bug being
fixed, so it has to fail loudly.

**03 removes `image_url` from a live response.** The proxy consumes it. That is a breaking contract
change, raised by the proxy team themselves — a user-facing list read should not carry a direct link
to infringing content.

**05 deliberately conflicts with invariant #1.** It needs `SearchFacesByImage`, which #1 forbids and
CI blocks. The narrowing is real and specific: identity may never come from a similarity score, but
attributing a face in a third-party photo to an already-enrolled candidate cannot corrupt an
identity. **Propose the reworded rule and wait for review before writing code.**
