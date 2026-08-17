variable "aws_region" {
  description = "Region for every resource here. Rekognition Face Liveness is not available in all regions."
  type        = string
  default     = "us-east-1"
}

# The live dev account's queues are already named imageshield-dev-* (twelve of
# them); this variable accepts only "development", which would render
# imageshield-development-* instead — a second, divergent naming scheme.
# Reconcile the two before `terraform apply` ever targets the dev account.
variable "environment" {
  description = "development | staging | production. Part of every resource name."
  type        = string

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "environment must be development, staging or production."
  }
}

variable "collection_id" {
  description = <<-EOT
    The Rekognition collection. Created HERE, never by hand: step 4's done-when
    asserts the collection holds exactly as many faces as active enrolments, and
    that is meaningless if nobody knows how the collection came to exist.
  EOT
  type        = string
  default     = "identity-v1"
}

variable "alarm_topic_arn" {
  description = "SNS topic every alarm publishes to. Empty disables alarm actions (local/dev plans)."
  type        = string
  default     = ""
}

variable "dlq_max_receive_count" {
  description = "Deliveries before a message is redriven to the DLQ. The relay and the search worker are both idempotent, so a few retries are free."
  type        = number
  default     = 5
}

variable "outbox_stall_seconds" {
  description = <<-EOT
    How long an unpublished outbox row may sit before alarming. This is the one
    that matters most on the queue side: the outbox failing silently means
    enrolments are not triggering searches, and NOTHING else would tell you.
  EOT
  type        = number
  default     = 300
}
