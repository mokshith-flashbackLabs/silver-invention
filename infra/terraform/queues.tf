# Two queues, a DLQ each, a redrive policy and a depth alarm.
#
# identity:index is reserved and unused in v1 (step 4 indexes synchronously
# inside the enrolment transaction). It exists because the outbox already
# names it and a queue that appears later is a deploy nobody planned.

locals {
  queues = {
    "identity-index" = { visibility_timeout = 60 }
    # Longer: a search run dispatches to N providers with retries and a
    # per-provider rate limit, so a claim can legitimately be held for minutes.
    # The store's stale-claim window (15 min) is the backstop above this.
    "search-runs" = { visibility_timeout = 600 }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queues

  name                      = "${local.name}-${each.key}-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum. A DLQ message is
  # evidence of a bug; losing it after four days
  # means losing the only copy of the failure.
}

resource "aws_sqs_queue" "main" {
  for_each = local.queues

  name                       = "${local.name}-${each.key}"
  visibility_timeout_seconds = each.value.visibility_timeout
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = var.dlq_max_receive_count
  })
}

# DLQ depth > 0 alarms. Not a threshold to tune: any message here is a message
# that failed every retry, and in this system that is either a poison payload
# or a bug. Both want a human.
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  for_each = var.alarm_topic_arn == "" ? {} : local.queues

  alarm_name          = "${local.name}-${each.key}-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  period              = 300
  statistic           = "Maximum"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq[each.key].name }
  alarm_actions       = [var.alarm_topic_arn]
  alarm_description   = "A message failed every retry. See docs/OPERATIONS.md, 'A DLQ has depth'."
}

# Age of the oldest message on the MAIN queue. Depth alone is a poor signal —
# a busy queue is deep and healthy — but a message nobody has consumed in ten
# minutes means the consumer is down or wedged.
resource "aws_cloudwatch_metric_alarm" "queue_age" {
  for_each = var.alarm_topic_arn == "" ? {} : local.queues

  alarm_name          = "${local.name}-${each.key}-oldest-message"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 600
  period              = 300
  statistic           = "Maximum"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.main[each.key].name }
  alarm_actions       = [var.alarm_topic_arn]
  alarm_description   = "Messages are not being consumed. Is the worker running?"
}

output "queue_urls" {
  description = "Set as SQS_IDENTITY_INDEX_URL and SQS_SEARCH_RUNS_URL."
  value       = { for key, queue in aws_sqs_queue.main : key => queue.url }
}
