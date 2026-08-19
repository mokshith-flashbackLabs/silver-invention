#!/usr/bin/env bash
set -euo pipefail

# SQS names can't contain ':' — these are CLAUDE.md §2's identity:index and
# search:runs queues, plus confirm:hits (protection-score design doc §7),
# mapped queue_name -> SQS name the same way in outbox.QUEUES / relay.py's
# _QUEUE_NAME_TO_CONFIG_FIELD.
queues=(
  imageshield-identity-index
  imageshield-search-runs
  imageshield-confirm-hits
)

for queue in "${queues[@]}"; do
  awslocal sqs create-queue --queue-name "$queue" >/dev/null
done

awslocal sqs list-queues
