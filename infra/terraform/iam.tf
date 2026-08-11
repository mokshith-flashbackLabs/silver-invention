# The service role.
#
# The policy document lives in policies/service-role.json, NOT inline here, so
# that tests/test_iam_policy.py can parse the same artifact this applies. A
# policy asserted from a copy is asserted about nothing.
#
# What is absent is the point: no s3: action of any kind, not even GetObject.
# CLAUDE.md §3.3 — the presigned-URL handshake exists so this service never
# needs S3 credentials, and with no grant a future "let's just read it from S3"
# cannot work even if somebody writes the code.

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type = "Service"
      # ECS tasks. The four processes — the API, the outbox relay, the
      # search:runs worker and the recheck worker — share one role: they are
      # one trust boundary, and splitting them would imply the recheck worker
      # holds something the API does not.
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "service" {
  name               = "${local.name}-service"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_policy" "service" {
  name        = "${local.name}-service"
  description = "ImageShield services. Deliberately grants no s3: action — see the policy file."

  policy = templatefile("${path.module}/policies/service-role.json", {
    region                   = var.aws_region
    account_id               = local.account_id
    collection_id            = var.collection_id
    identity_index_queue_arn = aws_sqs_queue.main["identity-index"].arn
    search_runs_queue_arn    = aws_sqs_queue.main["search-runs"].arn
    identity_index_dlq_arn   = aws_sqs_queue.dlq["identity-index"].arn
    search_runs_dlq_arn      = aws_sqs_queue.dlq["search-runs"].arn
    environment              = var.environment
  })
}

resource "aws_iam_role_policy_attachment" "service" {
  role       = aws_iam_role.service.name
  policy_arn = aws_iam_policy.service.arn
}

output "service_role_arn" {
  value = aws_iam_role.service.arn
}
