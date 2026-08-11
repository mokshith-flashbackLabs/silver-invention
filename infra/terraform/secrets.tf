# Secrets Manager, matching the one pattern the old system got right — it is
# the single thing in that codebase with no findings against it.
#
# Terraform creates the CONTAINER and never the VALUE. A secret value in a .tf
# file is a secret in git history, and `terraform state` holds values in plain
# text besides. Set them with `aws secretsmanager put-secret-value` after apply;
# OPERATIONS.md says so too.

locals {
  secrets = {
    "service-token"       = "X-Service-Token the proxy sends on every call."
    "admin-service-token" = "X-Admin-Service-Token. MUST differ from the above — boot refuses if they match."
    "database-url"        = "Postgres connection URL."
    "hive-api-key"        = "Hive Web Search. NOT Media Search — which product a key hits is decided by the Hive PROJECT it belongs to, not the URL."
    "google-credentials"  = "Google Vision service account JSON."
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each = local.secrets

  name        = "imageshield/${var.environment}/${each.key}"
  description = each.value

  # Long enough that a rotation mistake is recoverable, short enough that a
  # leaked value does not linger. Rotation here is deploy-both-sides-together;
  # there is no in-flight protocol and this step does not invent one.
  recovery_window_in_days = 7
}

output "secret_arns" {
  value = { for key, secret in aws_secretsmanager_secret.this : key => secret.arn }
}
