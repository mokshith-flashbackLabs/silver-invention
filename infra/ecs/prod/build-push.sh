#!/usr/bin/env bash
#
# Build the services image and push it to the PRODUCTION registry.
#
#   infra/ecs/prod/build-push.sh                # tag = short SHA of HEAD
#   infra/ecs/prod/build-push.sh --dry-run      # print the plan, build nothing
#   infra/ecs/prod/build-push.sh d97fff7        # explicit tag (must match HEAD)
#
# WHY THIS EXISTS. Until 2026-09-17 the build was prose in
# docs/deploy/DEPLOY-RUNBOOK.md §2, typed by hand each time. Three things went
# wrong on the first production push, and all three are mechanical:
#
#   1. The us-east-1 repository is IMMUTABLE (ap-south-1 dev is MUTABLE, which
#      is why this never surfaced in dev). Pushing a tag that already exists
#      fails the manifest PUT with a bare `400 Bad Request` that names neither
#      immutability nor the tag.
#
#   2. Worse, buildx exits 1 on a push that SUCCEEDED: it writes the tag, then
#      issues a follow-up manifest PUT to the same tag, which immutability
#      rejects. The layers and the tag are already in the registry. Anyone
#      following §2's "buildx exit MUST be 0" concludes the push failed — and
#      the intuitive next move, delete the tag and re-push, turns a non-problem
#      into an outage.
#
#   3. buildx attaches a provenance attestation by default, which forces the
#      image to be wrapped in an OCI index and leaves orphaned 0-byte manifests
#      behind on each retry. The backend's image is a single plain manifest.
#      --provenance=false matches it.
#
# So this script refuses the collision UP FRONT with a message that names the
# cause, and — the load-bearing part — decides success from `describe-images`
# against the registry rather than from buildx's exit code. The exit code is
# reported, never trusted.
#
# Modelled on render.sh next door, deliberately: same refusal style, same
# habit of naming the thing that is wrong rather than letting AWS give a
# generic error hours from the cause.
#
set -euo pipefail

export AWS_PAGER=""
export AWS_DEFAULT_REGION=us-east-1
# Git Bash rewrites anything that looks like an absolute path. Nothing here is
# path-shaped today, but a future --query or ARN argument would be mangled
# silently. Same guard render.sh carries.
export MSYS_NO_PATHCONV=1

ACCOUNT=225989356895
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com"
REPO=imageshield/services

# The Dockerfile pins linux/arm64 in BOTH stages as a constant — the cluster
# runs Graviton (t4g.medium) and the builder stage is where the venv installs
# psycopg[binary] and Pillow, so a host-arch build ships amd64 wheels into an
# arm64 runtime. The platform is therefore a property of the Dockerfile, not a
# choice made here; this constant exists to VERIFY what was pushed, not to
# select it.
EXPECT_ARCH=arm64

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

DRY_RUN=""
TAG=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*)        echo "unknown flag: $arg" >&2; exit 1 ;;
    *)         TAG="$arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# 1. The tag must mean exactly one commit.
# ---------------------------------------------------------------------------
# A tag built from a dirty tree names a commit whose contents were never what
# shipped. ECS caches by tag and the repository is immutable, so that lie is
# permanent and unfixable without burning the tag.
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is dirty — refusing to build" >&2
  echo "  a tag must name exactly one commit; commit or stash first:" >&2
  git status --short >&2
  exit 1
fi

HEAD_TAG="$(git rev-parse --short HEAD)"
if [[ -z "$TAG" ]]; then
  TAG="$HEAD_TAG"
elif [[ "$TAG" != "$HEAD_TAG" ]]; then
  echo "tag '$TAG' does not match HEAD ('$HEAD_TAG') — refusing" >&2
  echo "  check out the commit you mean to ship, then run this with no argument" >&2
  exit 1
fi

IMAGE="$REGISTRY/$REPO:$TAG"

# ---------------------------------------------------------------------------
# 2. Refuse a tag that already exists, BEFORE spending 40 minutes on a build.
# ---------------------------------------------------------------------------
# This is failure mode (1) above. describe-images exits non-zero with
# ImageNotFoundException when the tag is absent, which is the case we want.
if existing=$(aws ecr describe-images --repository-name "$REPO" \
      --image-ids imageTag="$TAG" \
      --query 'imageDetails[0].imageDigest' --output text 2>/dev/null); then
  echo "tag '$TAG' already exists in $REPO ($AWS_DEFAULT_REGION)" >&2
  echo "  digest: $existing" >&2
  echo "" >&2
  echo "  This repository is IMMUTABLE. A tag cannot be overwritten, and a push" >&2
  echo "  that tries fails with a bare 400 that explains none of this." >&2
  echo "" >&2
  echo "  If that image is already the one you want, there is nothing to do —" >&2
  echo "  verify it with:" >&2
  echo "    aws ecr describe-images --repository-name $REPO --image-ids imageTag=$TAG" >&2
  echo "  If you genuinely need to replace it, delete the tag first (deleting" >&2
  echo "  frees the name; immutability only blocks overwriting a live tag)." >&2
  exit 1
fi

echo "repository  $REGISTRY/$REPO"
echo "tag         $TAG  (HEAD, clean tree)"
echo "platform    linux/$EXPECT_ARCH  (pinned in the Dockerfile)"
echo "attestation disabled  (--provenance=false, single plain manifest)"

if [[ -n "$DRY_RUN" ]]; then
  echo ""
  echo "--dry-run: tag is free and the tree is clean; nothing built or pushed."
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Build and push.
# ---------------------------------------------------------------------------
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
echo "logged in to $REGISTRY"

# NOT piped. §12.2: a pipeline returns the filter's status, so a failed push
# reports as a pass. The exit code is captured here and REPORTED below, but the
# verdict comes from the registry — see §12.10 for why it cannot be trusted.
set +e
docker buildx build \
  --platform "linux/$EXPECT_ARCH" \
  --provenance=false --sbom=false \
  -t "$IMAGE" . --push
BUILDX_EXIT=$?
set -e

echo ""
echo "buildx exit code: $BUILDX_EXIT  (informational — the registry decides)"

# ---------------------------------------------------------------------------
# 4. The verdict, from the registry.
# ---------------------------------------------------------------------------
# This is the whole point of the script. buildx's exit code is unreliable
# against an immutable repository; what matters is whether a correctly-shaped
# image is addressable at this tag.
if ! digest=$(aws ecr describe-images --repository-name "$REPO" \
      --image-ids imageTag="$TAG" \
      --query 'imageDetails[0].imageDigest' --output text 2>/dev/null); then
  echo "PUSH FAILED — no image is tagged '$TAG' in $REPO" >&2
  echo "  buildx exited $BUILDX_EXIT; the registry has nothing at this tag." >&2
  exit 1
fi

size=$(aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$TAG" \
        --query 'imageDetails[0].imageSizeInBytes' --output text)
if [[ -z "$size" || "$size" == "None" || "$size" -eq 0 ]]; then
  echo "PUSH INCOMPLETE — '$TAG' exists but ECR reports size $size" >&2
  echo "  ECR sums the layer blobs it holds; zero means they are not all there." >&2
  exit 1
fi

# Architecture, read back from the registry. A wrong-arch image starts and then
# dies as `exec format error`, which reads like a broken entrypoint (§12.1) —
# an hour of debugging the wrong thing. Cheaper to catch here.
arch=$(docker buildx imagetools inspect "$IMAGE" --format '{{.Image.Architecture}}' 2>/dev/null || true)
if [[ -z "$arch" ]]; then
  # Older buildx, or an index rather than a plain manifest: fall back to the
  # human-readable form, which prints `Platform: linux/arm64`.
  arch=$(docker buildx imagetools inspect "$IMAGE" 2>/dev/null \
         | sed -n 's#.*Platform:[[:space:]]*linux/\([a-z0-9]*\).*#\1#p' | head -1)
fi
if [[ -z "$arch" ]]; then
  echo "PUSHED, BUT ARCHITECTURE UNVERIFIED — could not read it back from the registry" >&2
  echo "  digest $digest, $size bytes. Check by hand before starting anything:" >&2
  echo "    docker buildx imagetools inspect $IMAGE" >&2
  exit 1
fi
if [[ "$arch" != "$EXPECT_ARCH" ]]; then
  echo "WRONG ARCHITECTURE — pushed '$arch', cluster needs '$EXPECT_ARCH'" >&2
  echo "  This image will fail on the Graviton host as 'exec format error'." >&2
  exit 1
fi

echo ""
echo "VERIFIED IN REGISTRY"
echo "  image    $IMAGE"
echo "  digest   $digest"
echo "  size     $size bytes"
echo "  arch     linux/$arch"
echo ""
echo "Next: register the task definitions against this tag —"
echo "  for f in migrate-services services services-worker confirm fetcher; do"
echo "    infra/ecs/prod/render.sh \"\$f\" $TAG"
echo "  done"
