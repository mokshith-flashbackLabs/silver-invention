# ImageShield services infrastructure.
#
# Committed, not clicked. If it was created in a console, it does not exist —
# the old system deployed five Lambdas by manual PowerShell Compress-Archive
# with no IaC at all, and nobody could say what was deployed or why.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # aws_rekognition_collection landed in 5.60. INFERRED from the provider
      # changelog rather than verified by `terraform init` — there is no
      # terraform binary in this environment, so the CI job in
      # .github/workflows/infra.yml is what actually proves this file parses.
      version = ">= 5.60"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      System    = "imageshield"
      ManagedBy = "terraform"
      Repo      = "image_flashbacklabs"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  name       = "imageshield-${var.environment}"
}
