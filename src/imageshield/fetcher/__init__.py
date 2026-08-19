"""The crop fetcher (ARCHITECTURE.md §3.7): a standalone deployable, on its
own egress path, with no VPC access to any internal service.

It is the only component in this repo that reads bytes from a hostile,
caller-supplied URL rather than the proxy's own S3 — a third-party page a
search provider reported, or an infringement's ``page_url``. Two jobs:

- ``POST /v1/fetch`` — the raw bytes, for the confirm worker's own use
  (rekognition_confirm's face-match call needs an ``Image.Bytes`` payload, and
  this repo holds no S3 credentials with which to hand Rekognition an
  ``S3Object`` instead).
- ``POST /v1/crop`` — a face crop, blurred by default, for the review UI and
  the reviewer's own eyes (ARCHITECTURE.md §2.4): a trained reviewer sees a
  face crop, never the full image.

**No ``database_url`` field anywhere in this package.** :class:`FetcherConfig`
(``fetcher/config.py``) is structurally incapable of connecting to Postgres —
not because nothing here happens to call it, but because the field does not
exist to misconfigure. Fetched bytes live in one request's memory
(:class:`~imageshield.fetcher.fetch.FetchedImage`) and are never written to
disk, a column, or a log (INVARIANTS #9).

The SSRF guard is ``imageshield.recheck.ssrf.address_refusal`` — the DNS +
global-address half of the recheck loop's egress guard, factored out so this
package can reuse it without also inheriting the recheck loop's domain
allowlist, which has no meaning here: this fetcher is handed whatever URL a
provider or an infringement row names, so the guard runs on every URL, not
just ones from a known corpus.
"""
