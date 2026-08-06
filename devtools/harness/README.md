# Local test harness — Face Liveness + Hive

Dev-only testbed. Real AWS Rekognition Face Liveness (us-east-1) and real Hive web search.
Nothing here ships; see `docs/superpowers/specs/2026-08-05-local-test-harness-design.md`.

## Prereqs

- AWS creds in `~/.aws/credentials` with Rekognition + STS access (the `mokshith_dev` user works).
- `HIVE_API_KEY` in the repo root `.env.local`.
- Node ≥ 20, and the repo venv (`.venv`) with dev deps installed.

## Run

```bash
# 1. build the frontend (first time / after UI changes)
cd devtools/harness/web
npm install
npm run build

# 2. start the server from the repo root
.venv/Scripts/python -m uvicorn server:app --port 8900 --app-dir devtools/harness

# 3. open http://localhost:8900
```

For frontend iteration, `npm run dev` serves on :5173 and proxies `/api` to :8900.

## Costs

Each completed liveness check ≈ $0.015 (now also `LIVENESS_COST_PER_CHECK_USD` in config — the
step-8 budget input). Hive calls bill per your key's plan.

## Step-3 service mode (real-device E2E)

The Liveness tab has two modes. "Via ImageShield service" drives the real step-3 endpoints with
the harness playing the proxy — the browser never talks to the service directly, matching the
production topology. `/api/fake-s3/*` stands in for proxy-minted presigned URLs: the service PUTs
the ReferenceImage/AuditImages there and the UI fetches them back, proving an object exists at the
stored `reference_image_uri`.

```bash
# 1. local stack (Postgres on :15433) + migrations
docker compose -f docker-compose.local.yml up -d
DATABASE_URL=postgresql://imageshield:imageshield@localhost:15433/imageshield \
  .venv/Scripts/python scripts/migrate.py up

# 2. the real service on :8000
.venv/Scripts/python -m imageshield

# 3. this harness on :8900 (serves the UI and plays the proxy)
.venv/Scripts/python -m uvicorn server:app --port 8900 --app-dir devtools/harness
```

Then open http://localhost:8900 → Liveness → "Via ImageShield service". A real face should come
back `passed` with the persisted reference image rendered from fake-s3; a photo of a face on
another screen should come back `failed`. The result card's buttons demonstrate the idempotency
contract: same-key retry replays the stored 200, a new key gets 410, and a second session create
after a pass gets 409 (passed-but-unconsumed).

Fake-s3 is in-memory: restarting the harness empties it (stored URIs will 404 afterwards — the
DB row still proves what was written).

## What we learned about Hive's API (for the Phase 4 adapter)

- Submission endpoint (all task-based products): `POST {HIVE_BASE_URL}/api/v2/task/sync`
  with header `authorization: token <key>`; image as multipart `media` field or form `url` field.
- The product for our use case is **Web Search** ("reverse image search", ~25B indexed images) —
  returns matching image URLs, backlinks, similarity `score` (0.5–1.0), and a `query quality`
  signal. Hive's separately-named "Media Search" matches movies/TV content instead — not ours.
- Which product a key hits is determined by the Hive **project** the key belongs to, not the URL.
- Record the verbatim response as `raw_payload` (CLAUDE.md §7.2); the harness shows it unparsed
  for exactly this reason.

## Observed: Google Web Detection (2026-08-06)

- Full/partial matches carry **no similarity score** (`score: null`) — a production adapter's
  score mapping must be category-based (full match / partial match / page), not numeric.
- Web entities name **famous people only** (knowledge-graph lookup); non-public figures get
  generic content labels. Google deliberately does not identify faces.
- Confirms CLAUDE.md §7.1 empirically: image-search providers (Google, Hive) find copies of a
  known photo; they can never find a *different* photo of the same person, so deepfake coverage
  requires a face-search provider — none is integrated yet.
