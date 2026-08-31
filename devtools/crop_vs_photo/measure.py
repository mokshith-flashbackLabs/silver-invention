"""Does a face crop find what the full photo finds? — THROWAWAY SPIKE.

Answers the one open question gating face-crop seeds
(docs/superpowers/specs/2026-08-31-face-crop-seeds-design.md §6.1). The design is
approved; nobody knows how Hive behaves on a face crop, and shipping an unmeasured
detection regression in a product whose §1 calls a false negative a broken promise
is not acceptable. So: measure, then build.

SPIKE WORK, per CLAUDE.md §8 — "throwaway harnesses, vendor evaluation ... belongs
in devtools/, outside the numbered steps. It does not advance the build order, and
it should not be reported as a step being complete." Delete this directory once the
number is recorded in the spec.

WHAT IT DOES, and why each piece is the production one

  1. DetectFaces on each photo, through boto3 directly. Real bounding boxes; a
     hand-drawn box would measure a crop production never makes.
  2. crop_to_face() IMPORTED FROM THE REPO, so the bytes sent are byte-identical
     to what attribution/ would send: same 25% margin, same clamping, same JPEG
     quality. Re-implementing the crop here would make the result unfalsifiable.
  3. Both providers through the EXISTING harness endpoints, which already accept
     an uploaded file as well as a URL. No hosting, no presigned URLs, no app
     boot — and therefore no argument with config.py's refusal to run anything
     but SEARCH_PROVIDER=stub in development.

WHAT IT COSTS: 2 shapes x N faces x 2 providers, plus 2 for the full photo.
Google is list-priced at 0.003500/call. Hive's cost_per_call_usd is still NULL
(migration 0009, deliberately — contract-priced with no measured figure in this
repo), so its half cannot be priced from here. At ten photos this is cents; the
missing Hive number is the real gap, and 0009's own FOLLOW-UP names it.

USAGE

  # 1. start the harness (it holds the keys, from .env.local)
  python devtools/harness/server.py

  # 2. run this against a directory of real group photos
  python devtools/crop_vs_photo/measure.py ~/photos --out devtools/crop_vs_photo/out

WHAT TO DO WITH THE ANSWER — the rule is fixed in the spec BEFORE the data
arrives, deliberately, so it cannot be rationalised afterwards. "Crops find less,
so keep seeding the full photo" is NOT an available conclusion: that reinstates
the transmission the design exists to prevent. What a bad number changes is the
scope text and what the product is told it is buying, not whether an
unconsenting face keeps being sent to two third parties.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

# The PRODUCTION crop. Imported, never reimplemented — see the module docstring.
from imageshield.attribution.crop import CropTooSmall, crop_to_face  # noqa: E402
from imageshield.attribution.models import BoundingBox  # noqa: E402

HARNESS = "http://127.0.0.1:8000"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# Below this, crop_to_face refuses anyway; listed so the summary can say WHY a
# face was skipped rather than silently dropping it.
_TIMEOUT = httpx.Timeout(120.0)


@dataclass
class ProviderResult:
    provider: str
    shape: str  # "photo" | "crop:<n>"
    urls: set[str] = field(default_factory=set)
    google_sections: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    raw: Any = None


def _detect_faces(client: Any, image: bytes) -> list[BoundingBox]:
    response = client.detect_faces(Image={"Bytes": image}, Attributes=["DEFAULT"])
    boxes = []
    for detail in response.get("FaceDetails") or []:
        box = detail.get("BoundingBox") or {}
        boxes.append(
            BoundingBox(
                x=float(box.get("Left", 0.0)),
                y=float(box.get("Top", 0.0)),
                w=float(box.get("Width", 0.0)),
                h=float(box.get("Height", 0.0)),
            )
        )
    return boxes


def _urls_from(node: Any, into: set[str]) -> None:
    """Every URL-ish string anywhere in a provider payload.

    Deliberately shape-agnostic. The comparison this spike makes is "did the crop
    find the same PAGES as the photo", and being strict about which key a URL sat
    under would make the answer depend on a provider's response shape rather than
    on what it found.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value.startswith("http"):
                into.add(value.split("?")[0])
            else:
                _urls_from(value, into)
    elif isinstance(node, list):
        for value in node:
            _urls_from(value, into)


async def _query(
    client: httpx.AsyncClient, provider: str, shape: str, image: bytes
) -> ProviderResult:
    result = ProviderResult(provider=provider, shape=shape)
    try:
        response = await client.post(
            f"{HARNESS}/api/{provider}/search",
            files={"media": (f"{shape}.jpg", image, "image/jpeg")},
        )
    except httpx.HTTPError as exc:
        result.error = f"transport: {type(exc).__name__}"
        return result
    if response.status_code != 200:
        # No body echoed: a provider error can carry the request, and the request
        # is somebody's face.
        result.error = f"http {response.status_code}"
        return result

    payload = response.json()
    result.raw = payload
    _urls_from(payload, result.urls)
    if provider == "google":
        web = payload.get("raw", {}).get("responses", [{}])[0].get("webDetection", {})
        for section in ("fullMatchingImages", "partialMatchingImages", "pagesWithMatchingImages"):
            result.google_sections[section] = len(web.get(section) or [])
    return result


async def measure(photo_dir: Path, out_dir: Path) -> int:
    photos = sorted(p for p in photo_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not photos:
        print(f"no images in {photo_dir}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    rekognition = boto3.client("rekognition")

    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for photo_path in photos:
            image = photo_path.read_bytes()
            boxes = _detect_faces(rekognition, image)
            print(f"\n{photo_path.name}: {len(boxes)} face(s) detected")
            if len(boxes) < 2:
                # The design only crops multi-face photos: on a single-face photo
                # the photo IS the subject's image. Measuring one would answer a
                # question nobody asked.
                print("  skipped — single-face photos are unchanged by the design")
                continue

            shapes: list[tuple[str, bytes]] = [("photo", image)]
            for index, box in enumerate(boxes):
                try:
                    shapes.append((f"crop:{index}", crop_to_face(image, box)))
                except CropTooSmall:
                    print(f"  crop:{index} skipped — too small to search (as in production)")

            results: dict[tuple[str, str], ProviderResult] = {}
            for provider in ("hive", "google"):
                for shape, payload in shapes:
                    outcome = await _query(client, provider, shape, payload)
                    results[(provider, shape)] = outcome
                    detail = outcome.error or f"{len(outcome.urls)} url(s)"
                    print(f"  {provider:7} {shape:9} {detail}")
                    (out_dir / f"{photo_path.stem}.{provider}.{shape.replace(':', '')}.json").write_text(
                        json.dumps(outcome.raw, indent=2)[:2_000_000], encoding="utf-8"
                    )

            # THE ONE NUMBER THIS SPIKE EXISTS FOR: of the pages the FULL PHOTO
            # found, how many did any crop also find? That is what "cropping
            # loses detection" means concretely.
            for provider in ("hive", "google"):
                photo_urls = results[(provider, "photo")].urls
                crop_urls: set[str] = set()
                for shape, _ in shapes:
                    if shape != "photo":
                        crop_urls |= results[(provider, shape)].urls
                kept = photo_urls & crop_urls
                rows.append(
                    {
                        "photo": photo_path.name,
                        "provider": provider,
                        "faces": len(boxes),
                        "photo_found": len(photo_urls),
                        "crops_found": len(crop_urls),
                        "photo_pages_kept_by_crops": len(kept),
                        "crop_only": len(crop_urls - photo_urls),
                    }
                )

    print("\n" + "=" * 78)
    print("photo                     provider  faces  photo  crops  kept  crop-only")
    print("-" * 78)
    for row in rows:
        print(
            f"{row['photo'][:24]:24}  {row['provider']:8}  {row['faces']:5}  "
            f"{row['photo_found']:5}  {row['crops_found']:5}  "
            f"{row['photo_pages_kept_by_crops']:4}  {row['crop_only']:9}"
        )

    print("\nPER PROVIDER — the decision-relevant totals:")
    for provider in ("hive", "google"):
        subset = [r for r in rows if r["provider"] == provider]
        found = sum(r["photo_found"] for r in subset)
        kept = sum(r["photo_pages_kept_by_crops"] for r in subset)
        extra = sum(r["crop_only"] for r in subset)
        share = f"{kept / found:.0%}" if found else "n/a (photo found nothing)"
        print(
            f"  {provider:7} full-photo pages: {found:4}  also found by a crop: {kept:4} "
            f"({share})  found ONLY by a crop: {extra}"
        )
    print(
        "\n'found ONLY by a crop' is the other half of the story: a face cropped out\n"
        "of a group shot and reposted alone is exactly what the full photo cannot\n"
        f"find. Raw payloads in {out_dir} — keep them; §7.2 wants raw_payload verbatim\n"
        "so a later recalibration can re-read what the provider actually said."
    )
    (out_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photos", type=Path, help="directory of real group photos")
    parser.add_argument("--out", type=Path, default=Path("devtools/crop_vs_photo/out"))
    args = parser.parse_args()
    if not args.photos.is_dir():
        print(f"{args.photos} is not a directory", file=sys.stderr)
        return 1
    return asyncio.run(measure(args.photos, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
