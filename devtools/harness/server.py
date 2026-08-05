"""Local test harness for AWS Face Liveness + Hive web search.

Dev-only. Runs on this laptop against real providers; nothing here ships.
See docs/superpowers/specs/2026-08-05-local-test-harness-design.md.

Run:
    .venv/Scripts/python -m uvicorn server:app --port 8900 --app-dir devtools/harness

Then open http://localhost:8900 (after building the frontend in web/).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import boto3  # noqa: TID251 — dev harness, not service code
import httpx
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
            raise HTTPException(status_code=422, detail=f"challenge must be one of {CHALLENGE_TYPES}")
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


# ------------------------------------------------------------------ static

DIST = Path(__file__).parent / "web" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
