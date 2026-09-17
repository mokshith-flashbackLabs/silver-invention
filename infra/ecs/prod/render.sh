#!/usr/bin/env bash
#
# Render a production task definition and register it.
#
#   infra/ecs/prod/render.sh services da897d6
#   infra/ecs/prod/render.sh migrate-services da897d6 --dry-run
#
# WHY THIS EXISTS. A task definition references a secret by full ARN, and AWS
# appends a random six-character suffix when the secret is created:
#
#   arn:aws:secretsmanager:us-east-1:225989356895:secret:imageshield/prod/hive-9fk5ly:api_key::
#                                                                              ^^^^^^^
#
# That suffix cannot be derived from the name, and a stale one committed to git
# fails as a generic execution-role error ("unable to pull secrets or registry
# auth") that names neither the secret nor the key — hours of debugging away
# from the cause. So the prod files carry ${SECRET_*} placeholders and the ARNs
# are looked up here, against the account, every time.
#
# The dev files in infra/ecs/ carry literal ARNs and are registered directly.
# They predate this script and dev's secrets are not being recreated; there is
# no second copy of anything here, because these placeholders exist only in
# infra/ecs/prod/.
#
# Modelled on the backend repo's deploy/render.sh, deliberately: one estate, one
# account, and the failure modes its comments describe were paid for once
# already.
#
set -euo pipefail

FAMILY="${1:?usage: render.sh <services|services-worker|confirm|fetcher|migrate-services> <image-tag> [--dry-run]}"
IMAGE_TAG="${2:?usage: render.sh <family> <image-tag> [--dry-run]}"
DRY_RUN="${3:-}"

export AWS_PAGER=""
export AWS_DEFAULT_REGION=us-east-1
# Git Bash rewrites anything that looks like an absolute path. Secret names are
# relative (imageshield/prod/hive) so they survive, but a future argument that
# is not would be silently mangled into C:/Program Files/Git/...
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$ROOT/infra/ecs/prod/${FAMILY}.json"
[[ -f "$SOURCE" ]] || { echo "no such task definition: $SOURCE" >&2; exit 1; }

# Refuse to register a file that still carries dev identifiers. Catches the
# realistic mistake: copying a dev file into prod/ and changing only the parts
# you remembered. It would register cleanly and then point production at dev's
# collections, dev's queues and dev's secrets — a failure that looks like a
# working deploy.
#
# x-notes is excluded because it is documentation, stripped before registering,
# and a note explaining how this file differs from dev trips every pattern here.
DEV_MARKERS='imageshield-dev|ap-south-1|identity-dev|discovered-dev|/imageshield/dev'
if grep -v '"x-notes"' "$SOURCE" | grep -qE "$DEV_MARKERS"; then
  echo "$SOURCE still carries dev identifiers:" >&2
  grep -v '"x-notes"' "$SOURCE" | grep -oE "$DEV_MARKERS" | sort -u >&2
  exit 1
fi

# list-secrets returns null for a miss rather than exiting non-zero, which would
# otherwise sail past `set -e` and substitute the literal string "null".
arn_for() {
  local name="$1" arn
  arn=$(aws secretsmanager list-secrets \
        --filters Key=name,Values="$name" \
        --query 'SecretList[0].ARN' --output text)
  if [[ -z "$arn" || "$arn" == "None" || "$arn" == "null" ]]; then
    echo "secret not found: $name (region $AWS_DEFAULT_REGION)" >&2
    echo "  infra/ecs/prod/README.md lists every name this repo references and" >&2
    echo "  the JSON key inside it that ECS extracts" >&2
    return 1
  fi
  printf '%s' "$arn"
}

export IMAGE_TAG

# Only look up what this family actually references, so rendering the fetcher —
# which holds one token and no database — does not fail over a Hive key it must
# never carry.
needs() { grep -q "\${$1}" "$SOURCE"; }

needs SECRET_DB_APP_SERVICES      && export SECRET_DB_APP_SERVICES=$(arn_for imageshield/prod/db/app_services)
needs SECRET_DB_MIGRATOR_SERVICES && export SECRET_DB_MIGRATOR_SERVICES=$(arn_for imageshield/prod/db/migrator_services)
needs SECRET_SERVICE_TOKEN_BACKEND && export SECRET_SERVICE_TOKEN_BACKEND=$(arn_for imageshield/prod/service-token/backend-to-services)
needs SECRET_SERVICE_TOKEN_ADMIN  && export SECRET_SERVICE_TOKEN_ADMIN=$(arn_for imageshield/prod/service-token/admin)
needs SECRET_HIVE                 && export SECRET_HIVE=$(arn_for imageshield/prod/hive)
needs SECRET_GOOGLE_VISION        && export SECRET_GOOGLE_VISION=$(arn_for imageshield/prod/google-vision)

# JSON has no comments, and register-task-definition rejects unknown parameters,
# so x-notes lives in the file and is stripped here. jq if present, python
# otherwise — this machine has no jq, and a deploy script that fails on a
# missing formatting tool is one that gets bypassed by hand at the worst moment.
strip_notes() {
  if command -v jq >/dev/null 2>&1; then
    jq 'del(."x-notes")'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; d=json.load(sys.stdin); d.pop("x-notes",None); json.dump(d,sys.stdout)'
  elif command -v python >/dev/null 2>&1; then
    python -c 'import json,sys; d=json.load(sys.stdin); d.pop("x-notes",None); json.dump(d,sys.stdout)'
  else
    echo "need jq or python to strip x-notes" >&2
    return 1
  fi
}

# envsubst replaces an UNSET variable with the empty string. For a secret that
# renders as ":api_key::" and the valueFrom check below catches it; for an
# ordinary value it renders as "" and nothing catches it — the task definition
# registers perfectly and the container refuses to boot on a config key it was
# handed blank. So every placeholder is checked for emptiness first, by name.
# x-notes is excluded for the same reason the dev-marker lint excludes it:
# prose describing a placeholder is not a placeholder, and envsubst blanks it
# inside a field that is stripped before registering anyway.
for var in $(grep -v '"x-notes"' "$SOURCE" | grep -oE '\$\{[A-Z_]+\}' | tr -d '${}' | sort -u); do
  if [[ -z "${!var:-}" ]]; then
    echo "render-time value \$$var is unset or empty — $SOURCE references it" >&2
    exit 1
  fi
done

rendered=$(envsubst < "$SOURCE" | strip_notes)

# A placeholder that survived substitution would register a task definition
# whose secret ARN is the literal string "${SECRET_HIVE}".
if grep -q '\${' <<<"$rendered"; then
  echo "unsubstituted placeholder in $SOURCE:" >&2
  grep -o '\${[A-Z_]*}' <<<"$rendered" | sort -u >&2
  exit 1
fi

# The other half of the same check: a secret with no arn_for line above renders
# as ":api_key::" — no surviving "${", so the guard above passes it, and ECS
# then gives the generic "unable to pull secrets" error this script exists to
# prevent. Every valueFrom must be a real ARN.
if bad=$(grep -o '"valueFrom": "[^"]*"' <<<"$rendered" | grep -v '"valueFrom": "arn:aws:secretsmanager:'); then
  echo "valueFrom is not a secretsmanager ARN in $SOURCE — a secret is missing its arn_for lookup:" >&2
  echo "$bad" >&2
  exit 1
fi

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "$rendered"
  exit 0
fi

aws ecs register-task-definition --cli-input-json "$rendered" \
  --query 'taskDefinition.taskDefinitionArn' --output text
