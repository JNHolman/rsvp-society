terraform {
  required_version = "~> 1.16.0"

  backend "s3" {
    bucket         = "rsvp-society-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "rsvp-terraform-locks"
    encrypt        = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

variable "members_table_name" {
  type    = string
  default = "rsvp-members"
}

variable "allowed_origins" {
  type = list(string)
  default = [
    "https://rsvpsociety.com",
    "https://www.rsvpsociety.com",
  ]
}


variable "quo_api_base_url" {
  type        = string
  default     = "https://api.quo.com"
  description = "Quo REST API base URL. Override only for controlled rollback/testing."
}

variable "quo_api_key_secret_id" {
  type    = string
  default = "rsvp/quo-api-key"
}

variable "quo_phone_number_id" {
  type        = string
  default     = "PNqC0tQSaI"
  description = "Quo/OpenPhone phone-number ID (PN...) used for outbound SMS. Defaults to Jade's number. Override via TF_VAR_quo_phone_number_id or tfvars only if the number changes."
}

variable "host_phone_1" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional host approval phone number. Set with TF_VAR_host_phone_1 or tfvars; do not hardcode personal numbers in source."
}

variable "host_phone_2" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional secondary host approval phone number. Set with TF_VAR_host_phone_2 or tfvars."
}

variable "host_phone_secret_id" {
  type        = string
  default     = "rsvp/host-phones"
  description = "Secrets Manager secret holding host approval phone number(s) in E.164. Accepts a JSON object {\"host_phone_1\":\"+1...\",\"host_phone_2\":\"+1...\"}, a JSON array, or a comma-separated string. Preferred over the plaintext host_phone_* vars; falls back to them if empty."
}


variable "claude_model" {
  type        = string
  default     = "claude-haiku-4-5-20251001"
  description = "Anthropic model used by Jade. Kept configurable so model retirement does not require a code release."
}

variable "admin_token_secret_id" {
  type    = string
  default = "rsvp/admin-token"
}

variable "webhook_secret_id" {
  type        = string
  default     = "rsvp/webhook-secret"
  description = "Secrets Manager ID for Quo webhook signing key. Configure before SMS go-live."
}

variable "cloudfront_origin_verify_header" {
  type        = string
  sensitive   = true
  description = "Random secret sent only by CloudFront to the API origin; set with TF_VAR_cloudfront_origin_verify_header. Use at least 32 random characters."
  validation {
    condition     = length(var.cloudfront_origin_verify_header) >= 32
    error_message = "cloudfront_origin_verify_header must contain at least 32 characters."
  }
}

variable "cloudfront_origin_verify_previous_header" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Temporary previous origin secret accepted by WAF during rotation; clear after CloudFront deployment completes."
  validation {
    condition     = var.cloudfront_origin_verify_previous_header == "" || length(var.cloudfront_origin_verify_previous_header) >= 32
    error_message = "cloudfront_origin_verify_previous_header must be empty or contain at least 32 characters."
  }
}

locals {
  allowed_origins_csv     = join(",", var.allowed_origins)
  primary_frontend_origin = "https://rsvpsociety.com"
  # CORS preflight (OPTIONS) always returns apex origin.
  # www.rsvpsociety.com MUST redirect to apex before any page loads — ensure
  # DNS/CloudFront serves a 301 from www to apex so the browser's origin is
  # always https://rsvpsociety.com when API calls are made.
  # If www → apex redirect is not in place, browser API calls from www will
  # fail the CORS preflight. This is a DNS/hosting configuration requirement,
  # not a code change — set up the redirect at your DNS provider or CloudFront.
  cors_allow_origin_expr     = "'${local.primary_frontend_origin}'"
  cors_methods               = "'GET,POST,PUT,DELETE,OPTIONS'"
  cors_headers               = "'content-type,x-admin-token'"
  cors_mock_request_template = <<-EOT
{"statusCode": 200}
EOT

  pending_approvals_arn = aws_dynamodb_table.pending_approvals.arn
}

# -----------------------------
# DynamoDB (source of truth)
# -----------------------------
resource "aws_dynamodb_table" "members" {
  name         = var.members_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "phone"
  deletion_protection_enabled = true

  attribute {
    name = "phone"
    type = "S"
  }

  # GSI on status — turns list_members_by_status() from a full-table scan into
  # a targeted Query. O(matching items) instead of O(all items). Required once
  # the member list grows past ~2k to avoid read unit waste on every admin load.
  attribute {
    name = "status"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}


# -----------------------------
# DynamoDB — Events (current event source of truth)
# -----------------------------
resource "aws_dynamodb_table" "events" {
  name         = "rsvp-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "eventId"
  deletion_protection_enabled = true

  attribute {
    name = "eventId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# -----------------------------
# DynamoDB — Event history
# -----------------------------
resource "aws_dynamodb_table" "event_history" {
  name         = "rsvp-event-history"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "historyPk"
  range_key    = "eventKey"
  deletion_protection_enabled = true

  attribute {
    name = "historyPk"
    type = "S"
  }

  attribute {
    name = "eventKey"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# -----------------------------
# DynamoDB — EventInvites (composite key: eventId + phone)
# -----------------------------
resource "aws_dynamodb_table" "event_invites" {
  name         = "rsvp-event-invites"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "eventId"
  range_key    = "phone"
  deletion_protection_enabled = true

  attribute {
    name = "eventId"
    type = "S"
  }

  attribute {
    name = "phone"
    type = "S"
  }

  attribute {
    name = "quoMessageId"
    type = "S"
  }

  attribute {
    name = "jobId"
    type = "S"
  }

  # GSI: look up all events a member was invited to (phone → eventId)
  global_secondary_index {
    name            = "phone-index"
    hash_key        = "phone"
    range_key       = "eventId"
    projection_type = "ALL"
  }

  # GSI: resolve late Quo delivery callbacks back to the exact invite/event row.
  global_secondary_index {
    name            = "quo-message-index"
    hash_key        = "quoMessageId"
    projection_type = "ALL"
  }


  # GSI: rebuild invite-job status without scanning the full invite table.
  global_secondary_index {
    name            = "jobId-index"
    hash_key        = "jobId"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# =============================================================================
# Pending Approvals — dedicated host approval queue (pk=hostPhone sk=memberPhone)
# Replaces broken begins_with query on rsvp-events partition key
# =============================================================================
resource "aws_dynamodb_table" "pending_approvals" {
  name         = "rsvp-pending-approvals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "hostPhone"
  range_key    = "memberPhone"

  attribute {
    name = "hostPhone"
    type = "S"
  }

  attribute {
    name = "memberPhone"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  tags = {
    Project = "rsvp-society"
  }
}


# -----------------------------
# IAM (Lambda execution)
# -----------------------------
# Per-function roles are defined in iam_per_function.tf.
# The legacy shared rsvp-lambda-role has been removed — all six Lambda
# functions reference their per-function role ARNs directly.

# -----------------------------
# Lambda packaging (one canonical bundle)
# -----------------------------
# backend/lambda/build_lambda.sh is the single source of truth for package
# contents. CI builds this ZIP before terraform init/validate/plan, and
# Terraform deploys those exact bytes instead of maintaining a second set of
# packaging exclusions.
locals {
  lambda_bundle_path = "${path.module}/lambda_bundle.zip"
}

resource "aws_lambda_function" "access_request" {
  function_name = "rsvp-access-request"
  role          = aws_iam_role.lambda_access_request.arn
  handler       = "access_request.handler"
  runtime       = "python3.13"

  filename         = local.lambda_bundle_path
  source_code_hash = filebase64sha256(local.lambda_bundle_path)

  timeout = 10

  environment {
    variables = {
      ENVIRONMENT                  = "prod"
      MEMBERS_TABLE_NAME           = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME            = aws_dynamodb_table.events.name
      PENDING_APPROVALS_TABLE_NAME = aws_dynamodb_table.pending_approvals.name
      ALLOWED_ORIGINS              = local.allowed_origins_csv
      SMS_ENABLED                  = "true"
      SMS_PROVIDER                 = "quo"
      QUO_API_KEY_SECRET_ID        = var.quo_api_key_secret_id
      QUO_API_BASE_URL            = var.quo_api_base_url
      QUO_PHONE_NUMBER_ID          = var.quo_phone_number_id
      HOST_PHONE_SECRET_ID         = var.host_phone_secret_id
      HOST_PHONE_1                 = var.host_phone_1
      HOST_PHONE_2                 = var.host_phone_2
    }
  }
}

resource "aws_lambda_function" "admin_handler" {
  function_name = "rsvp-admin-handler"
  role          = aws_iam_role.lambda_admin_handler.arn
  handler       = "admin_handler.handler"
  runtime       = "python3.13"

  filename         = local.lambda_bundle_path
  source_code_hash = filebase64sha256(local.lambda_bundle_path)

  timeout = 60 # increased for bulk import

  environment {
    variables = {
      ENVIRONMENT              = "prod"
      MEMBERS_TABLE_NAME       = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME        = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME       = aws_dynamodb_table.event_invites.name
      EVENT_HISTORY_TABLE_NAME = aws_dynamodb_table.event_history.name
      CHECKINS_TABLE_NAME          = aws_dynamodb_table.checkins.name
      AUDIT_LOG_TABLE_NAME         = aws_dynamodb_table.audit_log.name
      PENDING_APPROVALS_TABLE_NAME = aws_dynamodb_table.pending_approvals.name
      ALLOWED_ORIGINS          = local.allowed_origins_csv
      ADMIN_TOKEN_SECRET_ID    = var.admin_token_secret_id

      # Enable welcome SMS on approval
      SMS_ENABLED              = "true"
      SMS_PROVIDER             = "quo"
      QUO_API_KEY_SECRET_ID       = var.quo_api_key_secret_id
      QUO_API_BASE_URL           = var.quo_api_base_url
      QUO_PHONE_NUMBER_ID         = var.quo_phone_number_id
      CLAUDE_API_KEY_SECRET_ID       = "rsvp/claude-api-key"
      CLAUDE_MODEL                   = var.claude_model
      WELCOME_REQUIRE_APPROVED       = "true"
      REMINDER_LAMBDA_ARN            = aws_lambda_function.reminder_handler.arn
      REMINDER_SCHEDULER_ROLE_ARN    = aws_iam_role.reminder_scheduler_invoker.arn
    }
  }
}

resource "aws_lambda_function" "sms_handler" {
  function_name = "rsvp-sms-handler"
  role          = aws_iam_role.lambda_sms_handler.arn
  handler       = "sms_handler.handler"
  runtime       = "python3.13"

  filename         = local.lambda_bundle_path
  source_code_hash = filebase64sha256(local.lambda_bundle_path)

  # 25s gives headroom: the Jade/Claude call is capped at 10s in _claude(),
  # leaving budget to still send the SMS reply if the model is slow.
  timeout = 25

  environment {
    variables = {
      ENVIRONMENT                  = "prod"
      MEMBERS_TABLE_NAME           = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME            = aws_dynamodb_table.events.name
      PENDING_APPROVALS_TABLE_NAME = aws_dynamodb_table.pending_approvals.name
      INVITES_TABLE_NAME           = aws_dynamodb_table.event_invites.name
      INVITE_JOBS_TABLE_NAME       = aws_dynamodb_table.invite_jobs.name
      CHECKINS_TABLE_NAME          = aws_dynamodb_table.checkins.name
      AUDIT_LOG_TABLE_NAME         = aws_dynamodb_table.audit_log.name
      ALLOWED_ORIGINS              = local.allowed_origins_csv
      SMS_ENABLED                  = "true"
      SMS_PROVIDER                 = "quo"
      QUO_API_KEY_SECRET_ID        = var.quo_api_key_secret_id
      QUO_API_BASE_URL            = var.quo_api_base_url
      QUO_PHONE_NUMBER_ID          = var.quo_phone_number_id
      CLAUDE_API_KEY_SECRET_ID     = "rsvp/claude-api-key"
      CLAUDE_MODEL                 = var.claude_model
      WEBHOOK_SECRET_ID            = var.webhook_secret_id
      WEBHOOK_SECRET_ID_2          = "rsvp/webhook-secret-delivery"
      HOST_PHONE_SECRET_ID         = var.host_phone_secret_id
      HOST_PHONE_1                 = var.host_phone_1
      HOST_PHONE_2                 = var.host_phone_2
    }
  }
}






resource "aws_lambda_function" "invite_handler" {
  function_name = "rsvp-invite-handler"
  role          = aws_iam_role.lambda_invite_handler.arn
  handler       = "invite_handler.handler"
  runtime       = "python3.13"

  filename         = local.lambda_bundle_path
  source_code_hash = filebase64sha256(local.lambda_bundle_path)

  timeout = 300 # 300 invites × 0.25s pacing + retries ≈ 90s typical, 300s max

  environment {
    variables = {
      ENVIRONMENT            = "prod"
      MEMBERS_TABLE_NAME     = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME      = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME     = aws_dynamodb_table.event_invites.name
      INVITE_JOBS_TABLE_NAME = aws_dynamodb_table.invite_jobs.name
      ALLOWED_ORIGINS        = local.allowed_origins_csv
      SMS_ENABLED            = "true"
      SMS_PROVIDER           = "quo"
      QUO_API_KEY_SECRET_ID  = var.quo_api_key_secret_id
      QUO_API_BASE_URL      = var.quo_api_base_url
      QUO_PHONE_NUMBER_ID    = var.quo_phone_number_id
      ADMIN_TOKEN_SECRET_ID  = var.admin_token_secret_id
      AUDIT_LOG_TABLE_NAME   = aws_dynamodb_table.audit_log.name
      AUTO_WAVE_SCHEDULER_ROLE_ARN = aws_iam_role.reminder_scheduler_invoker.arn
      INVITE_HANDLER_ARN     = "arn:aws:lambda:${local.region}:${local.account}:function:rsvp-invite-handler"
    }
  }
}

# -----------------------------
# API Gateway REST API (v1)
# -----------------------------
resource "aws_api_gateway_rest_api" "api" {
  name = "rsvp-api"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# Resources
resource "aws_api_gateway_resource" "access" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "access"
}

resource "aws_api_gateway_resource" "admin" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "admin"
}

resource "aws_api_gateway_resource" "admin_members" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "members"
}

resource "aws_api_gateway_resource" "admin_members_status" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "status"
}

resource "aws_api_gateway_resource" "sms" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "sms"
}

resource "aws_api_gateway_resource" "sms_inbound" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.sms.id
  path_part   = "inbound"
}

# -----------------------------
# Methods + Integrations (Lambda Proxy)
# -----------------------------
# /access POST
resource "aws_api_gateway_method" "access_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.access.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "access_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.access.id
  http_method             = aws_api_gateway_method.access_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.access_request.invoke_arn
}

# /admin/members GET
resource "aws_api_gateway_method" "admin_members_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members.id
  http_method             = aws_api_gateway_method.admin_members_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members DELETE
resource "aws_api_gateway_method" "admin_members_delete" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members.id
  http_method   = "DELETE"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_delete" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members.id
  http_method             = aws_api_gateway_method.admin_members_delete.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/status POST
resource "aws_api_gateway_method" "admin_members_status_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_status.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_status_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_status.id
  http_method             = aws_api_gateway_method.admin_members_status_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /sms/inbound POST (kept for later)
resource "aws_api_gateway_method" "sms_inbound_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.sms_inbound.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "sms_inbound_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.sms_inbound.id
  http_method             = aws_api_gateway_method.sms_inbound_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.sms_handler.invoke_arn
}



# /event
resource "aws_api_gateway_resource" "event" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "event"
}

# /event/current (public)
resource "aws_api_gateway_resource" "event_current" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.event.id
  path_part   = "current"
}

resource "aws_api_gateway_method" "event_current_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event_current.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_current_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.event_current.id
  http_method             = aws_api_gateway_method.event_current_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /health (public — no auth, liveness check)
resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "health"
}

resource "aws_api_gateway_method" "health_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.health.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "health_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.health.id
  http_method             = aws_api_gateway_method.health_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/event
resource "aws_api_gateway_resource" "admin_event" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "event"
}

resource "aws_api_gateway_method" "admin_event_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event.id
  http_method             = aws_api_gateway_method.admin_event_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_event_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event.id
  http_method             = aws_api_gateway_method.admin_event_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_event_delete" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "DELETE"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_delete" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event.id
  http_method             = aws_api_gateway_method.admin_event_delete.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/invite
resource "aws_api_gateway_resource" "admin_invite" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "invite"
}

# /admin/invite/preview
resource "aws_api_gateway_resource" "admin_invite_preview" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_invite.id
  path_part   = "preview"
}

# /admin/invite/send
resource "aws_api_gateway_resource" "admin_invite_send" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_invite.id
  path_part   = "send"
}

# /admin/members/gender
resource "aws_api_gateway_resource" "admin_members_gender" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "gender"
}

# /admin/members/tier
resource "aws_api_gateway_resource" "admin_members_tier" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "tier"
}

# /admin/members/attendance
resource "aws_api_gateway_resource" "admin_members_attendance" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "attendance"
}

# /admin/members/import
resource "aws_api_gateway_resource" "admin_members_import" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "import"
}

# Methods + integrations
resource "aws_api_gateway_method" "admin_invite_preview_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_preview.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_preview_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_invite_preview.id
  http_method             = aws_api_gateway_method.admin_invite_preview_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.invite_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_invite_send_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_send.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_send_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_invite_send.id
  http_method             = aws_api_gateway_method.admin_invite_send_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.invite_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_gender_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_gender.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_gender_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_gender.id
  http_method             = aws_api_gateway_method.admin_members_gender_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_tier_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_tier.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_tier_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_tier.id
  http_method             = aws_api_gateway_method.admin_members_tier_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_attendance_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_attendance.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_attendance_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_attendance.id
  http_method             = aws_api_gateway_method.admin_members_attendance_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/import POST
resource "aws_api_gateway_method" "admin_members_import_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_import.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_import_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_import.id
  http_method             = aws_api_gateway_method.admin_members_import_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/import OPTIONS (CORS preflight)
resource "aws_api_gateway_method" "admin_members_import_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_import.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_import_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_import.id
  http_method = aws_api_gateway_method.admin_members_import_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}
resource "aws_api_gateway_method_response" "admin_members_import_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_import.id
  http_method = aws_api_gateway_method.admin_members_import_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_import_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_import.id
  http_method = aws_api_gateway_method.admin_members_import_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_import_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
  depends_on = [
    aws_api_gateway_integration.admin_members_import_options,
    aws_api_gateway_method_response.admin_members_import_options_200,
  ]
}



# /admin/members/search
resource "aws_api_gateway_resource" "admin_members_search" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "search"
}

# /admin/members/search GET
resource "aws_api_gateway_method" "admin_members_search_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_search.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_search_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_search.id
  http_method             = aws_api_gateway_method.admin_members_search_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/search OPTIONS (CORS preflight)
resource "aws_api_gateway_method" "admin_members_search_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_search.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_search_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_search.id
  http_method = aws_api_gateway_method.admin_members_search_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "admin_members_search_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_search.id
  http_method = aws_api_gateway_method.admin_members_search_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_search_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_search.id
  http_method = aws_api_gateway_method.admin_members_search_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_search_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
  depends_on = [
    aws_api_gateway_integration.admin_members_search_options,
    aws_api_gateway_method_response.admin_members_search_options_200,
  ]
}

# /event GET (public — no auth, admin_handler always strips venue/address/logistics)
resource "aws_api_gateway_method" "event_public_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_public_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.event.id
  http_method             = aws_api_gateway_method.event_public_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# -----------------------------
# CORS (OPTIONS) - required for browser calls
# -----------------------------
# Helper: we set method response headers, then mock integration returns them.

# /access OPTIONS
resource "aws_api_gateway_method" "access_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.access.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "access_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.access.id
  http_method = aws_api_gateway_method.access_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "access_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.access.id
  http_method = aws_api_gateway_method.access_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "access_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.access.id
  http_method = aws_api_gateway_method.access_options.http_method
  status_code = aws_api_gateway_method_response.access_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = "'content-type'"
  }
}


# /admin/members/history
resource "aws_api_gateway_resource" "admin_members_history" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "history"
}

# /admin/members/history GET
resource "aws_api_gateway_method" "admin_members_history_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_history.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_history_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_history.id
  http_method             = aws_api_gateway_method.admin_members_history_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/history OPTIONS (CORS preflight)
resource "aws_api_gateway_method" "admin_members_history_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_history.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_history_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_history.id
  http_method = aws_api_gateway_method.admin_members_history_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "admin_members_history_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_history.id
  http_method = aws_api_gateway_method.admin_members_history_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_history_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_history.id
  http_method = aws_api_gateway_method.admin_members_history_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_history_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
  }

  depends_on = [
    aws_api_gateway_integration.admin_members_history_options,
    aws_api_gateway_method_response.admin_members_history_options_200,
  ]
}

# /admin/members OPTIONS
resource "aws_api_gateway_method" "admin_members_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members.id
  http_method = aws_api_gateway_method.admin_members_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "admin_members_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members.id
  http_method = aws_api_gateway_method.admin_members_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members.id
  http_method = aws_api_gateway_method.admin_members_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

# /admin/members/status OPTIONS
resource "aws_api_gateway_method" "admin_members_status_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_status.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_status_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_status.id
  http_method = aws_api_gateway_method.admin_members_status_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "admin_members_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_status.id
  http_method = aws_api_gateway_method.admin_members_status_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_status.id
  http_method = aws_api_gateway_method.admin_members_status_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_status_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

# /sms/inbound OPTIONS (for later)
resource "aws_api_gateway_method" "sms_inbound_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.sms_inbound.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "sms_inbound_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.sms_inbound.id
  http_method = aws_api_gateway_method.sms_inbound_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "sms_inbound_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.sms_inbound.id
  http_method = aws_api_gateway_method.sms_inbound_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "sms_inbound_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.sms_inbound.id
  http_method = aws_api_gateway_method.sms_inbound_options.http_method
  status_code = aws_api_gateway_method_response.sms_inbound_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = "'content-type'"
  }
}

# -----------------------------
# Deployment + Stage
# -----------------------------
resource "aws_api_gateway_deployment" "deploy" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeploy = sha1(join("", [
      filesha1("${path.module}/main.tf"),
      filesha1("${path.module}/eventbridge.tf"),
      filesha1("${path.module}/confirmed_endpoint.tf"),
      filesha1("${path.module}/audit_log.tf"),
      filesha1("${path.module}/analytics_endpoint.tf"),
      filesha1("${path.module}/events_endpoint.tf"),
      filesha1("${path.module}/draft_message_endpoint.tf"),
      filesha1("${path.module}/cloudwatch_dashboard.tf"),
    ]))
  }
  depends_on = [
    # /access
    aws_api_gateway_integration.access_post,
    aws_api_gateway_integration.access_options,
    aws_api_gateway_integration_response.access_options_200,
    # /sms/inbound
    aws_api_gateway_integration.sms_inbound_post,
    aws_api_gateway_integration.sms_inbound_options,
    aws_api_gateway_integration_response.sms_inbound_options_200,
    # /event (public)
    aws_api_gateway_integration.event_public_get,
    aws_api_gateway_integration.event_public_options,
    aws_api_gateway_integration_response.event_public_options_200,
    # /health
    aws_api_gateway_integration.health_get,
    # /event/current
    aws_api_gateway_integration.event_current_get,
    aws_api_gateway_integration.event_current_options,
    aws_api_gateway_integration_response.event_current_options_200,
    # /admin/members
    aws_api_gateway_integration.admin_members_get,
    aws_api_gateway_integration.admin_members_delete,
    aws_api_gateway_integration.admin_members_options,
    aws_api_gateway_integration_response.admin_members_options_200,
    # /admin/members/status
    aws_api_gateway_integration.admin_members_status_post,
    aws_api_gateway_integration.admin_members_status_options,
    aws_api_gateway_integration_response.admin_members_status_options_200,
    # /admin/members/gender
    aws_api_gateway_integration.admin_members_gender_post,
    aws_api_gateway_integration.admin_members_gender_options,
    aws_api_gateway_integration_response.admin_members_gender_options_200,
    # /admin/members/tier
    aws_api_gateway_integration.admin_members_tier_post,
    aws_api_gateway_integration.admin_members_tier_options,
    aws_api_gateway_integration_response.admin_members_tier_options_200,
    # /admin/members/attendance
    aws_api_gateway_integration.admin_members_attendance_post,
    aws_api_gateway_integration.admin_members_attendance_options,
    aws_api_gateway_integration_response.admin_members_attendance_options_200,
    # /admin/members/import
    aws_api_gateway_integration.admin_members_import_post,
    aws_api_gateway_integration.admin_members_import_options,
    aws_api_gateway_integration_response.admin_members_import_options_200,
    # /admin/members/search
    aws_api_gateway_integration.admin_members_search_get,
    aws_api_gateway_integration.admin_members_search_options,
    aws_api_gateway_integration_response.admin_members_search_options_200,
    # /admin/members/history
    aws_api_gateway_integration.admin_members_history_get,
    aws_api_gateway_integration.admin_members_history_options,
    aws_api_gateway_integration_response.admin_members_history_options_200,
    # /admin/members/confirmed
    aws_api_gateway_integration.admin_members_confirmed_get,
    aws_api_gateway_integration.admin_members_confirmed_options,
    aws_api_gateway_integration_response.admin_members_confirmed_options_200,
    # /admin/event
    aws_api_gateway_integration.admin_event_get,
    aws_api_gateway_integration.admin_event_post,
    aws_api_gateway_integration.admin_event_delete,
    aws_api_gateway_integration.admin_event_options,
    aws_api_gateway_integration_response.admin_event_options_200,
    # /admin/invite/preview
    aws_api_gateway_integration.admin_invite_preview_post,
    aws_api_gateway_integration.admin_invite_preview_options,
    aws_api_gateway_integration_response.admin_invite_preview_options_200,
    # /admin/invite/send
    aws_api_gateway_integration.admin_invite_send_post,
    aws_api_gateway_integration.admin_invite_send_options,
    aws_api_gateway_integration_response.admin_invite_send_options_200,
    # /admin/invite/status
    aws_api_gateway_integration.admin_invite_status_get,
    aws_api_gateway_integration.admin_invite_status_options,
    aws_api_gateway_integration_response.admin_invite_status_options_200,
    # /admin/invite/reminder
    aws_api_gateway_integration.admin_invite_reminder_post,
    aws_api_gateway_integration.admin_invite_reminder_options,
    aws_api_gateway_integration_response.admin_invite_reminder_options_200,
    # /admin/event/analytics
    aws_api_gateway_integration.admin_event_analytics_get,
    aws_api_gateway_integration.admin_event_analytics_options,
    aws_api_gateway_integration_response.admin_event_analytics_options_200,
    # /admin/events
    aws_api_gateway_integration.admin_events_get,
    aws_api_gateway_integration.admin_events_post,
    aws_api_gateway_integration.admin_events_delete,
    aws_api_gateway_integration.admin_events_set_active_post,
    aws_api_gateway_integration.admin_events_archive_post,
    aws_api_gateway_integration.admin_events_duplicate_post,
    aws_api_gateway_integration.admin_events_finalize_attendance_post,
    aws_api_gateway_integration.admin_events_options,
    aws_api_gateway_integration_response.admin_events_options_200,
    # /admin/event/draft-message
    aws_api_gateway_integration.admin_event_draft_message_post,
    aws_api_gateway_integration.admin_event_draft_message_options,
    aws_api_gateway_integration_response.admin_event_draft_message_options_200,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.deploy.id
  stage_name    = "prod"
}

# -----------------------------
# Lambda permissions for API Gateway
# -----------------------------
resource "aws_lambda_permission" "allow_apigw_access" {
  statement_id  = "AllowApiGwInvokeAccess"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.access_request.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/POST/access"
}

resource "aws_lambda_permission" "allow_apigw_admin" {
  statement_id  = "AllowApiGwInvokeAdmin"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.admin_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*/admin*"
}

resource "aws_lambda_permission" "allow_apigw_sms" {
  statement_id  = "AllowApiGwInvokeSms"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sms_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/POST/sms/inbound"
}



resource "aws_lambda_permission" "allow_apigw_invite" {
  statement_id  = "AllowApiGwInvokeInvite"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.invite_handler.function_name
  principal     = "apigateway.amazonaws.com"
  # Method wildcard so GET /admin/invite/status is permitted, not just POST sends.
  source_arn = "${aws_api_gateway_rest_api.api.execution_arn}/*/*/admin/invite/*"
}

# -----------------------------
# WAF (rate limit /access) + Association (valid for REST API stage)
# -----------------------------
resource "aws_wafv2_web_acl" "api_acl" {
  name  = "rsvp-api-acl"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "rsvp-api-acl"
    sampled_requests_enabled   = true
  }

  rule {
    name     = "block-untrusted-api-origin"
    priority = 0

    action {
      block {}
    }

    statement {
      not_statement {
        statement {
          or_statement {
            statement {
              byte_match_statement {
                search_string = var.cloudfront_origin_verify_header
                field_to_match {
                  single_header {
                    name = "x-rsvp-origin-verify"
                  }
                }
                positional_constraint = "EXACTLY"
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            # AWS WAF requires at least two OR statements. With no old key, the second match repeats the current key.
            dynamic "statement" {
              for_each = [var.cloudfront_origin_verify_previous_header != "" ? var.cloudfront_origin_verify_previous_header : var.cloudfront_origin_verify_header]
              content {
                byte_match_statement {
                  search_string = statement.value
                  field_to_match {
                    single_header {
                      name = "x-rsvp-origin-verify"
                    }
                  }
                  positional_constraint = "EXACTLY"
                  text_transformation {
                    priority = 0
                    type     = "NONE"
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "block-untrusted-api-origin"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-access"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 60
        aggregate_key_type = "IP"
        forwarded_ip_config {
          header_name       = "X-Forwarded-For"
          fallback_behavior = "MATCH"
        }

        scope_down_statement {
          byte_match_statement {
            search_string = "/access"
            field_to_match {
              uri_path {}
            }
            positional_constraint = "STARTS_WITH"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit-access"
      sampled_requests_enabled   = true
    }
  }

  # Protect paid/expensive admin actions without throttling invite-status polling.
  # 20 calls per evaluation window is well above legitimate use for send/reminder/draft.
  rule {
    name     = "rate-limit-admin-blast"
    priority = 2

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 20
        aggregate_key_type = "IP"
        forwarded_ip_config {
          header_name       = "X-Forwarded-For"
          fallback_behavior = "MATCH"
        }

        scope_down_statement {
          or_statement {
            statement {
              byte_match_statement {
                search_string = "/admin/invite/send"
                field_to_match {
                  uri_path {}
                }
                positional_constraint = "STARTS_WITH"
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string = "/admin/invite/reminder"
                field_to_match {
                  uri_path {}
                }
                positional_constraint = "STARTS_WITH"
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string = "/admin/event/draft-message"
                field_to_match {
                  uri_path {}
                }
                positional_constraint = "STARTS_WITH"
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit-admin-blast"
      sampled_requests_enabled   = true
    }
  }

  # General admin abuse ceiling. Specific paid/expensive actions above remain stricter.
  # This also covers event/Jade/admin routes that were previously outside WAF protection.
  rule {
    name     = "rate-limit-admin-general"
    priority = 3

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 120
        aggregate_key_type = "IP"
        forwarded_ip_config {
          header_name       = "X-Forwarded-For"
          fallback_behavior = "MATCH"
        }

        scope_down_statement {
          byte_match_statement {
            search_string = "/admin/"
            field_to_match {
              uri_path {}
            }
            positional_constraint = "STARTS_WITH"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit-admin-general"
      sampled_requests_enabled   = true
    }
  }
}

resource "aws_wafv2_web_acl_association" "api_stage" {
  resource_arn = "arn:aws:apigateway:${data.aws_region.current.name}::/restapis/${aws_api_gateway_rest_api.api.id}/stages/${aws_api_gateway_stage.prod.stage_name}"
  web_acl_arn  = aws_wafv2_web_acl.api_acl.arn
}

# -----------------------------
# CloudWatch Log Groups (retention)
# -----------------------------
resource "aws_cloudwatch_log_group" "access_request" {
  name              = "/aws/lambda/rsvp-access-request"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "admin_handler" {
  name              = "/aws/lambda/rsvp-admin-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "sms_handler" {
  name              = "/aws/lambda/rsvp-sms-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "reminder_handler" {
  name              = "/aws/lambda/rsvp-reminder-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "invite_handler" {
  name              = "/aws/lambda/rsvp-invite-handler"
  retention_in_days = 30
}


output "api_base_url" {
  value = "https://api.rsvpsociety.com"
}

# -----------------------------
# CORS OPTIONS — /admin/invite/preview
# -----------------------------
resource "aws_api_gateway_method" "admin_invite_preview_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_preview.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_invite_preview_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_preview.id
  http_method = aws_api_gateway_method.admin_invite_preview_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "admin_invite_preview_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_preview.id
  http_method = aws_api_gateway_method.admin_invite_preview_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_invite_preview_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_preview.id
  http_method = aws_api_gateway_method.admin_invite_preview_options.http_method
  status_code = aws_api_gateway_method_response.admin_invite_preview_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

# -----------------------------
# CORS OPTIONS — /admin/invite/send
# -----------------------------
resource "aws_api_gateway_method" "admin_invite_send_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_send.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_invite_send_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_send.id
  http_method = aws_api_gateway_method.admin_invite_send_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = local.cors_mock_request_template
  }
}

resource "aws_api_gateway_method_response" "admin_invite_send_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_send.id
  http_method = aws_api_gateway_method.admin_invite_send_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_invite_send_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_send.id
  http_method = aws_api_gateway_method.admin_invite_send_options.http_method
  status_code = aws_api_gateway_method_response.admin_invite_send_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

# =============================================================================
# /admin/invite/status — poll async blast job status
# =============================================================================
resource "aws_api_gateway_resource" "admin_invite_status" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_invite.id
  path_part   = "status"
}
resource "aws_api_gateway_method" "admin_invite_status_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_status.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_status_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_invite_status.id
  http_method             = aws_api_gateway_method.admin_invite_status_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.invite_handler.invoke_arn
}
resource "aws_api_gateway_method_response" "admin_invite_status_get_200" {
  rest_api_id         = aws_api_gateway_rest_api.api.id
  resource_id         = aws_api_gateway_resource.admin_invite_status.id
  http_method         = aws_api_gateway_method.admin_invite_status_get.http_method
  status_code         = "200"
  response_parameters = { "method.response.header.Access-Control-Allow-Origin" = true }
}
resource "aws_api_gateway_method" "admin_invite_status_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_status.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_status_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_invite_status.id
  http_method       = aws_api_gateway_method.admin_invite_status_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_invite_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_status.id
  http_method = aws_api_gateway_method.admin_invite_status_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_invite_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_status.id
  http_method = aws_api_gateway_method.admin_invite_status_options.http_method
  status_code = aws_api_gateway_method_response.admin_invite_status_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}


# -----------------------------
# CORS OPTIONS — missing endpoints (batch fix)
# -----------------------------

resource "aws_api_gateway_method" "admin_event_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_event.id
  http_method       = aws_api_gateway_method.admin_event_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_event_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event.id
  http_method = aws_api_gateway_method.admin_event_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_event_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event.id
  http_method = aws_api_gateway_method.admin_event_options.http_method
  status_code = aws_api_gateway_method_response.admin_event_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "admin_members_gender_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_gender.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_gender_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_members_gender.id
  http_method       = aws_api_gateway_method.admin_members_gender_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_members_gender_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_gender.id
  http_method = aws_api_gateway_method.admin_members_gender_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_gender_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_gender.id
  http_method = aws_api_gateway_method.admin_members_gender_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_gender_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "admin_members_tier_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_tier.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_tier_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_members_tier.id
  http_method       = aws_api_gateway_method.admin_members_tier_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_members_tier_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_tier.id
  http_method = aws_api_gateway_method.admin_members_tier_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_tier_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_tier.id
  http_method = aws_api_gateway_method.admin_members_tier_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_tier_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "admin_members_attendance_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_attendance.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_attendance_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_members_attendance.id
  http_method       = aws_api_gateway_method.admin_members_attendance_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_members_attendance_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_attendance.id
  http_method = aws_api_gateway_method.admin_members_attendance_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_attendance_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_attendance.id
  http_method = aws_api_gateway_method.admin_members_attendance_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_attendance_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "event_current_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event_current.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_current_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.event_current.id
  http_method       = aws_api_gateway_method.event_current_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "event_current_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_current.id
  http_method = aws_api_gateway_method.event_current_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "event_current_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_current.id
  http_method = aws_api_gateway_method.event_current_options.http_method
  status_code = aws_api_gateway_method_response.event_current_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "event_public_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_public_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.event.id
  http_method       = aws_api_gateway_method.event_public_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "event_public_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event.id
  http_method = aws_api_gateway_method.event_public_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "event_public_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event.id
  http_method = aws_api_gateway_method.event_public_options.http_method
  status_code = aws_api_gateway_method_response.event_public_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}
