# Alarms.
#
# Most of the per-provider numbers (call count, cost, success rate, latency
# percentiles, breaker state, budget headroom) are computed in Postgres by
# providers/observability.py and read through GET /v1/admin/providers/health.
# They are not CloudWatch metrics, because they are derived from rows the
# service already writes and duplicating them into a metric stream would create
# a second source of truth that can disagree with the first.
#
# What lives HERE is what CloudWatch can see and Postgres cannot: queue health,
# and the alarms that need to fire when the service itself is not running to
# report on itself.
#
# The queue alarms are in queues.tf, beside the queues they watch.

resource "aws_cloudwatch_log_group" "service" {
  name              = "/imageshield/${var.environment}"
  retention_in_days = 30
}

# THE ONE THAT MATTERS MOST (step-9 §6, and OPERATIONS.md's longest runbook).
#
# A provider returning zero successful calls for 24h looks EXACTLY like a quiet
# week for infringements. In a safety product that means users are being told
# they are clear when nothing actually looked. Nothing else in the system
# distinguishes the two — providers_succeeded is the only field that does, and
# nobody reads it per-run.
#
# Emitted by the service as a metric filter over its own structured logs rather
# than polled, so it fires even if the admin API is down.
resource "aws_cloudwatch_log_metric_filter" "provider_success" {
  name           = "${local.name}-provider-success"
  log_group_name = aws_cloudwatch_log_group.service.name
  pattern        = "{ $.event = \"search.run_completed\" && $.providers_succeeded IS NOT NULL }"

  metric_transformation {
    name      = "ProviderSuccessfulRuns"
    namespace = "ImageShield/${var.environment}"
    value     = "1"
    # 0 rather than no-value: the alarm below treats "missing" as breaching,
    # but an explicit zero is what makes a 24h flat line visible on a graph
    # somebody is looking at for another reason.
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "no_successful_provider_calls" {
  count = var.alarm_topic_arn == "" ? 0 : 1

  alarm_name          = "${local.name}-no-successful-provider-calls-24h"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 86400
  statistic           = "Sum"
  namespace           = "ImageShield/${var.environment}"
  metric_name         = "ProviderSuccessfulRuns"
  # Missing data BREACHES here, unlike everywhere else. No data means no runs
  # completed, which is the outage this alarm exists to catch — treating it as
  # "insufficient data" would make the alarm silent in exactly the case it
  # matters.
  treat_missing_data = "breaching"
  alarm_actions      = [var.alarm_topic_arn]
  alarm_description  = "No provider has succeeded in 24h. Users may be being told they are clear when nothing looked. See docs/OPERATIONS.md."
}

# The CSAM tripwire (docs/OPERATIONS.md §10). Until now the "ops alarm" for a
# quarantined hit was the log line itself -- a human watching, with no paging
# path. Same pattern as provider_success above: a metric filter over the
# confirm worker's structured logs, so it fires even if nobody is tailing
# CloudWatch Logs Insights.
resource "aws_cloudwatch_log_metric_filter" "confirm_quarantined" {
  name           = "${local.name}-confirm-quarantined"
  log_group_name = aws_cloudwatch_log_group.service.name
  pattern        = "{ $.event = \"confirm.quarantined\" }"

  metric_transformation {
    name          = "ConfirmQuarantined"
    namespace     = "ImageShield/${var.environment}"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "confirm_quarantined" {
  count = var.alarm_topic_arn == "" ? 0 : 1

  alarm_name          = "${local.name}-confirm-quarantined"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "ImageShield/${var.environment}"
  metric_name         = "ConfirmQuarantined"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alarm_topic_arn]
  alarm_description   = "A hit was quarantined. See docs/OPERATIONS.md §10."
}

# The outbox failing silently: enrolments are not triggering searches, and
# nothing else would tell you. Emitted by the relay.
resource "aws_cloudwatch_log_metric_filter" "outbox_lag" {
  name           = "${local.name}-outbox-lag"
  log_group_name = aws_cloudwatch_log_group.service.name
  pattern        = "{ $.event = \"outbox.lag_seconds\" }"

  metric_transformation {
    name      = "OutboxOldestUnpublishedSeconds"
    namespace = "ImageShield/${var.environment}"
    value     = "$.lag_seconds"
  }
}

resource "aws_cloudwatch_metric_alarm" "outbox_stalled" {
  count = var.alarm_topic_arn == "" ? 0 : 1

  alarm_name          = "${local.name}-outbox-stalled"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = var.outbox_stall_seconds
  period              = 300
  statistic           = "Maximum"
  namespace           = "ImageShield/${var.environment}"
  metric_name         = "OutboxOldestUnpublishedSeconds"
  alarm_actions       = [var.alarm_topic_arn]
  alarm_description   = "Outbox rows unpublished for too long. Enrolments are not triggering searches."
}
