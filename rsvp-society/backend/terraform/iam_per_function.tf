# =============================================================================
# iam_per_function.tf
#
# Replaces the single shared rsvp-lambda-role with per-function IAM roles,
# each scoped to only the tables and secrets that function actually needs.
#
# WHY: The shared role meant a compromise of any one Lambda (e.g. sms_handler
# receiving a malicious payload) could read/write every table and secret.
# Per-function roles limit blast radius to only what that function touches.
#
# HOW TO MIGRATE:
#   1. Add this file to your terraform directory.
#   2. Update each aws_lambda_function resource in main.tf to reference the
#      matching per-function role ARN (substitutions listed below).
#   3. Remove the old aws_iam_role.lambda_role and aws_iam_policy.lambda_policy
#      resources from main.tf once all Lambdas are migrated.
#   4. terraform plan → review → apply.
#
# LAMBDA ROLE SUBSTITUTIONS (replace `role` in each aws_lambda_function):
#   access_request  → aws_iam_role.lambda_access_request.arn
#   admin_handler   → aws_iam_role.lambda_admin_handler.arn
#   sms_handler     → aws_iam_role.lambda_sms_handler.arn
#   invite_handler  → aws_iam_role.lambda_invite_handler.arn
#   reminder_handler → aws_iam_role.lambda_reminder_handler.arn
#   event_handler   → aws_iam_role.lambda_event_handler.arn
# =============================================================================

locals {
  log_actions = [
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
  ]
  # All table ARNs for reference in each policy block
  members_arn      = aws_dynamodb_table.members.arn
  events_arn       = aws_dynamodb_table.events.arn
  invites_arn      = aws_dynamodb_table.event_invites.arn
  invites_index    = "${aws_dynamodb_table.event_invites.arn}/index/*"
  checkins_arn     = aws_dynamodb_table.checkins.arn
  region           = data.aws_region.current.name
  account          = data.aws_caller_identity.current.account_id
}

# ── Shared assume-role policy (all Lambdas use this) ──────────────────────
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# =============================================================================
# access_request — public sign-up endpoint
# Needs: members (write), Secrets Manager (QUO key for welcome SMS)
# =============================================================================
resource "aws_iam_role" "lambda_access_request" {
  name               = "rsvp-fn-access-request"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_access_request" {
  name = "policy"
  role = aws_iam_role.lambda_access_request.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [local.members_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.quo_api_key_secret_id}*",
        ]
      },
    ]
  })
}

# =============================================================================
# admin_handler — internal admin panel backend
# Needs: members (full), events (full), invites (full), checkins (write),
#        Secrets Manager (admin token + QUO key for any triggered SMS)
# =============================================================================
resource "aws_iam_role" "lambda_admin_handler" {
  name               = "rsvp-fn-admin-handler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_admin_handler" {
  name = "policy"
  role = aws_iam_role.lambda_admin_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = "*" },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
        ]
        Resource = [
          local.members_arn,
          local.events_arn,
          local.invites_arn,
          local.invites_index,
          local.checkins_arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.admin_token_secret_id}*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.quo_api_key_secret_id}*",
        ]
      },
    ]
  })
}

# =============================================================================
# sms_handler — inbound SMS / Jade AI replies
# Needs: members (read + opt-out write), invites (read + status write),
#        events (read), Secrets Manager (QUO key + Claude API key)
# Does NOT need checkins or admin token.
# =============================================================================
resource "aws_iam_role" "lambda_sms_handler" {
  name               = "rsvp-fn-sms-handler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_sms_handler" {
  name = "policy"
  role = aws_iam_role.lambda_sms_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [local.members_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [local.invites_arn, local.invites_index]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.quo_api_key_secret_id}*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:rsvp/claude-api-key*",
        ]
      },
    ]
  })
}

# =============================================================================
# invite_handler — blast invites to approved members
# Needs: members (read + invitedCount update), invites (write), events (read),
#        Secrets Manager (admin token + QUO key)
# Does NOT need checkins or member delete.
# =============================================================================
resource "aws_iam_role" "lambda_invite_handler" {
  name               = "rsvp-fn-invite-handler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_invite_handler" {
  name = "policy"
  role = aws_iam_role.lambda_invite_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
        Resource = [local.members_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [local.invites_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.admin_token_secret_id}*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.quo_api_key_secret_id}*",
        ]
      },
    ]
  })
}

# =============================================================================
# reminder_handler — scheduled + manual reminder SMS
# Needs: members (read), invites (read), events (read),
#        Secrets Manager (admin token + QUO key)
# Does NOT need checkins, member write, or invite write.
# =============================================================================
resource "aws_iam_role" "lambda_reminder_handler" {
  name               = "rsvp-fn-reminder-handler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_reminder_handler" {
  name = "policy"
  role = aws_iam_role.lambda_reminder_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [local.members_arn, local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = [local.invites_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.admin_token_secret_id}*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.quo_api_key_secret_id}*",
        ]
      },
    ]
  })
}

# =============================================================================
# event_handler — public + admin event read/write
# Needs: events (read + write), Secrets Manager (admin token)
# Does NOT need members, invites, checkins, or SMS keys.
# =============================================================================
resource "aws_iam_role" "lambda_event_handler" {
  name               = "rsvp-fn-event-handler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_event_handler" {
  name = "policy"
  role = aws_iam_role.lambda_event_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.admin_token_secret_id}*",
        ]
      },
    ]
  })
}
