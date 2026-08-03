#!/usr/bin/env bash
set -euo pipefail

# SQS names can't contain ':' — these are CLAUDE.md §2's identity:index and
# search:runs queues.
queues=(
  imageshield-identity-index
  imageshield-search-runs
)

for queue in "${queues[@]}"; do
  awslocal sqs create-queue --queue-name "$queue" >/dev/null
done

awslocal sqs list-queues
