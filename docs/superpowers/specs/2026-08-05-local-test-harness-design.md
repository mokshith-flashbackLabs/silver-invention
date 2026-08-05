# Design: local test harness for Face Liveness + Hive media search

**Date:** 2026-08-05
**Status:** Approved (user), building
**Depends on:** the liveness vendor decision (same folder, 2026-08-05) — AWS is the sole authority.

## Purpose

A local, dev-only testbed to exercise the two external integrations v1 depends on — AWS Rekognition
Face Liveness and Hive media search — with **real** provider calls, before the production service
endpoints are built. Lets us validate the provider relationship, the credential wiring, response
shapes, and the UX realities (oval challenge, color flashes, retry behaviour) on this machine.

Not a prototype of the production service. Nothing under `devtools/` ships, and the production
rules that bind `src/imageshield` (proxy-only ingress, service tokens, no public surface) do not
apply to it — it is a laptop-local tool.

## Shape

```
devtools/harness/
  README.md          how to run
  server.py          FastAPI app, port 8900
  web/               Vite + React frontend
    src/…            two tabs: Liveness, Hive
    dist/            built output, served by server.py (gitignored)
```

### Backend (`server.py`, reuses the repo venv — fastapi/boto3/httpx already present)

| Route | Does |
|---|---|
| `POST /api/liveness/sessions` | `CreateFaceLivenessSession` (us-east-1), returns `session_id` |
| `GET /api/liveness/sessions/{id}/result` | `GetFaceLivenessSessionResults` → status, confidence, pass/fail vs `LIVENESS_MIN_CONFIDENCE`, reference image as a data URI |
| `GET /api/aws-creds` | STS `GetSessionToken` → short-lived creds for the browser's liveness component |
| `POST /api/hive/search` | Forwards an image (upload or URL) to Hive media search with `HIVE_API_KEY` from `.env.local`; returns parsed matches + raw payload |
| `GET /` | serves `web/dist` |

Config comes from `.env.local` (Hive key, threshold) and the machine's AWS shared-credentials file
(`mokshith_dev`, account 225989356895). CORS: localhost only, since the Vite dev server may run on
another port.

### Frontend (Vite + React)

- **Liveness tab**: start check → real `@aws-amplify/ui-react-liveness` `FaceLivenessDetector`
  (webcam, oval, color flashes, streamed to Rekognition) → result card: confidence, pass/fail,
  reference image. Credentials come from `/api/aws-creds` via the component's `credentialProvider`
  hook — the long-lived key never reaches the browser.
- **Hive tab**: image upload or URL → `/api/hive/search` → match list (URL, score) + collapsible
  raw JSON.

## Rules that still apply, even in dev tooling

- **No image persistence** (invariant #9 in spirit): reference images and uploads stay in memory /
  the HTTP response; nothing written to disk.
- **No secrets in the frontend or logs**: Hive key stays server-side; AWS creds crossing to the
  browser are STS-temporary.
- Each liveness check costs ~$0.015; the UI shows a running check count as a reminder.

## Out of scope

Enrolment (`IndexFaces`), `DeleteFaces`, the proxy contract, service-token auth, persistence of any
kind. Those belong to the production build (NEAR-TERM-BUILD.md Part 1), not the harness.

## Open item resolved at build time

Hive media search's exact endpoint/response shape — pulled from docs.thehive.ai during
implementation; whatever is learned lands in the harness README for the Phase 4 adapter work.
