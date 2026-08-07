"""Local test harness for AWS Face Liveness + Hive web search.

Dev-only. Runs on this laptop against real providers; nothing here ships.
See docs/superpowers/specs/2026-08-05-local-test-harness-design.md.

Run:
    .venv/Scripts/python -m uvicorn server:app --port 8900 --app-dir devtools/harness

Then open http://localhost:8900 (after building the frontend in web/).

For the step-3 real-device E2E, the harness also plays the PROXY in front of
the real ImageShield service (``python -m imageshield`` on :8000):

- ``/api/service/liveness/*`` forwards to the service with the local
  ``SERVICE_TOKEN`` — the browser never talks to the service directly, same
  topology as production (Client -> Proxy -> Services);
- ``/api/fake-s3/{key}`` is the stand-in for proxy-minted presigned URLs:
  the service PUTs the ReferenceImage/AuditImages here and the UI GETs them
  back, which is how "an object exists at that URI" gets verified locally.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import boto3
import httpx
from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[2]
AWS_REGION = "us-east-1"


def _load_env_local() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = REPO_ROOT / ".env.local"
    if not env_path.exists():
        raise RuntimeError(f"{env_path} not found — the harness needs HIVE_API_KEY from it")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


ENV = _load_env_local()
HIVE_API_KEY = ENV.get("HIVE_API_KEY", "")
HIVE_BASE_URL = ENV.get("HIVE_BASE_URL", "https://api.thehive.ai").rstrip("/")
LIVENESS_MIN_CONFIDENCE = float(ENV.get("LIVENESS_MIN_CONFIDENCE", "90"))
GOOGLE_VISION_API_KEY = ENV.get("GOOGLE_VISION_API_KEY", "")
GOOGLE_VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"

# The real ImageShield service (step 3), started separately with
# `.venv/Scripts/python -m imageshield`. The harness forwards to it as the
# proxy would, carrying the shared service token from .env.local.
SERVICE_BASE_URL = ENV.get("SERVICE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SERVICE_TOKEN = ENV.get("SERVICE_TOKEN", "")
# Where the service reaches THIS harness for the fake presigned PUTs.
HARNESS_BASE_URL = ENV.get("HARNESS_BASE_URL", "http://127.0.0.1:8900").rstrip("/")

rekognition = boto3.client("rekognition", region_name=AWS_REGION)
sts = boto3.client("sts", region_name=AWS_REGION)

app = FastAPI(title="ImageShield local harness", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- liveness


CHALLENGE_TYPES = ("FaceMovementAndLightChallenge", "FaceMovementChallenge")


@app.post("/api/liveness/sessions")
def create_liveness_session(challenge: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if challenge:
        if challenge not in CHALLENGE_TYPES:
            raise HTTPException(
                status_code=422, detail=f"challenge must be one of {CHALLENGE_TYPES}"
            )
        kwargs["Settings"] = {"ChallengePreferences": [{"Type": challenge}]}
    resp = rekognition.create_face_liveness_session(**kwargs)
    return {"session_id": resp["SessionId"], "region": AWS_REGION, "challenge": challenge}


@app.get("/api/liveness/sessions/{session_id}/result")
def liveness_result(session_id: str) -> dict[str, Any]:
    try:
        resp = rekognition.get_face_liveness_session_results(SessionId=session_id)
    except rekognition.exceptions.SessionNotFoundException:
        raise HTTPException(status_code=404, detail="session not found") from None

    confidence = resp.get("Confidence")
    status = resp["Status"]
    reference_image = None
    ref = resp.get("ReferenceImage") or {}
    if ref.get("Bytes"):
        b64 = base64.b64encode(ref["Bytes"]).decode("ascii")
        reference_image = f"data:image/jpeg;base64,{b64}"

    return {
        "status": status,
        "confidence": confidence,
        "threshold": LIVENESS_MIN_CONFIDENCE,
        "passed": status == "SUCCEEDED"
        and confidence is not None
        and confidence >= LIVENESS_MIN_CONFIDENCE,
        "reference_image": reference_image,  # data URI, shown in the UI, never persisted
        "audit_image_count": len(resp.get("AuditImages") or []),
    }


@app.get("/api/aws-creds")
def aws_creds() -> dict[str, str]:
    """Short-lived STS creds so the browser's FaceLivenessDetector can stream
    video to Rekognition without ever seeing the long-lived key."""
    resp = sts.get_session_token(DurationSeconds=3600)
    c = resp["Credentials"]
    return {
        "accessKeyId": c["AccessKeyId"],
        "secretAccessKey": c["SecretAccessKey"],
        "sessionToken": c["SessionToken"],
        "expiration": c["Expiration"].isoformat(),
    }


# ------------------------------------------- step-3 service (harness-as-proxy)

# In production the proxy mints presigned S3 PUT/GET URLs. Locally there is
# deliberately no S3 anywhere near this repo, so the harness IS the bucket:
# an in-memory dict keyed by object path. Restarting the harness empties it.
FAKE_S3: dict[str, bytes] = {}


@app.put("/api/fake-s3/{key:path}")
async def fake_s3_put(key: str, request: Request) -> dict[str, Any]:
    FAKE_S3[key] = await request.body()
    return {"stored": key, "bytes": len(FAKE_S3[key])}


@app.get("/api/fake-s3/{key:path}")
def fake_s3_get(key: str) -> Response:
    data = FAKE_S3.get(key)
    if data is None:
        raise HTTPException(status_code=404, detail="no such object")
    return Response(content=data, media_type="image/jpeg")


def _service_headers() -> dict[str, str]:
    if not SERVICE_TOKEN:
        raise HTTPException(
            status_code=500, detail="SERVICE_TOKEN missing from .env.local"
        )
    return {"X-Service-Token": SERVICE_TOKEN}


def _passthrough(resp: httpx.Response) -> JSONResponse:
    """Return the service's response verbatim (status + body) so the UI shows
    the real contract — 201/200/400/404/409/410/429 and the error envelope."""
    try:
        body = resp.json()
    except ValueError:
        body = {"non_json_body": resp.text[:2000]}
    return JSONResponse(status_code=resp.status_code, content=body)


# The last Idempotency-Key used per session, so the UI can demonstrate the
# difference between a same-key retry (200 replay) and a new-key replay (410).
_LAST_RESULT_KEY: dict[str, str] = {}


@app.post("/api/service/liveness/sessions")
async def service_create_session(payload: dict[str, Any]) -> JSONResponse:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SERVICE_BASE_URL}/v1/liveness/sessions",
            json={"user_ref": payload.get("user_ref")},
            headers=_service_headers(),
        )
    return _passthrough(resp)


@app.post("/api/service/liveness/{session_id}/result")
async def service_post_result(
    session_id: str, payload: dict[str, Any] | None = None
) -> JSONResponse:
    reuse_key = bool(payload and payload.get("reuse_key"))
    if reuse_key and session_id in _LAST_RESULT_KEY:
        idempotency_key = _LAST_RESULT_KEY[session_id]
    else:
        idempotency_key = str(uuid.uuid4())
        _LAST_RESULT_KEY[session_id] = idempotency_key

    prefix = f"liveness/{session_id}"
    reference_put_url = f"{HARNESS_BASE_URL}/api/fake-s3/{prefix}/reference.jpg"
    audit_put_urls = [
        f"{HARNESS_BASE_URL}/api/fake-s3/{prefix}/audit-{i}.jpg" for i in range(4)
    ]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{SERVICE_BASE_URL}/v1/liveness/{session_id}/result",
            json={"reference_put_url": reference_put_url, "audit_put_urls": audit_put_urls},
            headers={**_service_headers(), "Idempotency-Key": idempotency_key},
        )
    out = _passthrough(resp)
    if resp.status_code == 200:
        # Hand the UI the GET URLs so it can prove the objects exist at the
        # stored URIs ("done when": an object exists at that URI).
        body = resp.json()
        body["reference_image_url"] = f"/api/fake-s3/{prefix}/reference.jpg"
        body["audit_image_urls"] = [
            f"/api/fake-s3/{prefix}/audit-{i}.jpg"
            for i in range(4)
            if f"{prefix}/audit-{i}.jpg" in FAKE_S3
        ]
        body["idempotency_key"] = idempotency_key
        return JSONResponse(status_code=200, content=body)
    return out


@app.get("/api/service/liveness/{session_id}")
async def service_get_session(session_id: str) -> JSONResponse:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{SERVICE_BASE_URL}/v1/liveness/{session_id}", headers=_service_headers()
        )
    return _passthrough(resp)


# -------------------------------------------------------------------- hive


def _hive_headers() -> dict[str, str]:
    return {"authorization": f"token {HIVE_API_KEY}"}


def _summarise_hive(payload: Any) -> list[dict[str, Any]]:
    """Best-effort flatten of match-like objects out of whatever shape the
    key's Hive project returns. The raw payload is always returned alongside."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            keys = {k.lower() for k in node}
            if ("url" in keys or "backlink" in keys or "backlinks" in keys) and (
                "score" in keys or "similarity_score" in keys
            ):
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return found[:100]


@app.post("/api/hive/search")
async def hive_search(
    media: UploadFile | None = None, url: str | None = Form(default=None)
) -> dict[str, Any]:
    if media is None and not url:
        raise HTTPException(status_code=422, detail="provide an image file ('media') or a 'url'")

    endpoint = f"{HIVE_BASE_URL}/api/v2/task/sync"
    async with httpx.AsyncClient(timeout=120) as client:
        if media is not None:
            content = await media.read()  # stays in memory only
            resp = await client.post(
                endpoint,
                headers=_hive_headers(),
                files={"media": (media.filename or "image.jpg", content, media.content_type)},
            )
        else:
            resp = await client.post(endpoint, headers=_hive_headers(), data={"url": url})

    try:
        payload = resp.json()
    except ValueError:
        payload = {"non_json_body": resp.text[:2000]}

    return {
        "http_status": resp.status_code,
        "matches": _summarise_hive(payload),
        "raw_payload": payload,
    }


# ------------------------------------------------------- google web detection


@app.post("/api/google/search")
async def google_search(
    media: UploadFile | None = None, url: str | None = Form(default=None)
) -> dict[str, Any]:
    """Google Cloud Vision WEB_DETECTION — image-search kind, like Hive.

    Response sections (see raw_payload): fullMatchingImages (exact copies),
    partialMatchingImages (crops/variants), pagesWithMatchingImages (backlink
    equivalent), visuallySimilarImages (loose), webEntities (what Google
    thinks the image depicts, with scores).
    """
    if not GOOGLE_VISION_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="GOOGLE_VISION_API_KEY missing — add it to .env.local and restart the harness",
        )
    if media is None and not url:
        raise HTTPException(status_code=422, detail="provide an image file ('media') or a 'url'")

    if media is not None:
        content = await media.read()  # stays in memory only
        image: dict[str, Any] = {"content": base64.b64encode(content).decode("ascii")}
    else:
        image = {"source": {"imageUri": url}}

    body = {
        "requests": [
            {"image": image, "features": [{"type": "WEB_DETECTION", "maxResults": 50}]}
        ]
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GOOGLE_VISION_ENDPOINT, params={"key": GOOGLE_VISION_API_KEY}, json=body
        )

    try:
        payload = resp.json()
    except ValueError:
        payload = {"non_json_body": resp.text[:2000]}

    wd = {}
    if isinstance(payload, dict):
        responses = payload.get("responses") or [{}]
        wd = responses[0].get("webDetection") or {}

    def flatten(section: str, score_key: str = "score") -> list[dict[str, Any]]:
        return [
            {"kind": section, "url": item.get("url", ""), "score": item.get(score_key)}
            for item in wd.get(section) or []
        ]

    matches = (
        flatten("fullMatchingImages")
        + flatten("partialMatchingImages")
        + flatten("pagesWithMatchingImages")
    )
    return {
        "http_status": resp.status_code,
        "matches": matches,
        # Google's guesses at image CONTENT (not matches) — noisy below ~0.5.
        "entities": sorted(
            (
                {"description": e.get("description"), "score": e.get("score")}
                for e in wd.get("webEntities") or []
                if e.get("description") and (e.get("score") or 0) >= 0.5
            ),
            key=lambda e: e["score"] or 0,
            reverse=True,
        ),
        "best_guess": [
            label.get("label") for label in wd.get("bestGuessLabels") or []
        ],
        "similar_count": len(wd.get("visuallySimilarImages") or []),
        "raw_payload": payload,
    }


# ------------------------------------------------------------------ static

DIST = Path(__file__).parent / "web" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
