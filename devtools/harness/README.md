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

Each completed liveness check ≈ $0.015. Hive calls bill per your key's plan.

## What we learned about Hive's API (for the Phase 4 adapter)

- Submission endpoint (all task-based products): `POST {HIVE_BASE_URL}/api/v2/task/sync`
  with header `authorization: token <key>`; image as multipart `media` field or form `url` field.
- The product for our use case is **Web Search** ("reverse image search", ~25B indexed images) —
  returns matching image URLs, backlinks, similarity `score` (0.5–1.0), and a `query quality`
  signal. Hive's separately-named "Media Search" matches movies/TV content instead — not ours.
- Which product a key hits is determined by the Hive **project** the key belongs to, not the URL.
- Record the verbatim response as `raw_payload` (CLAUDE.md §7.2); the harness shows it unparsed
  for exactly this reason.
