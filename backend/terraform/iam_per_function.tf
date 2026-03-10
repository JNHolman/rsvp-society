# =============================================================================
# iam_per_function.tf
#
# Per-function IAM roles — each scoped to only the tables and secrets that
# function actually needs.
#
# LAMBDA ROLE SUBSTITUTIONS (replace `role` in each aws_lambda_function):
#   access_request   → aws_iam_role.lambda_access_request.arn
#   admin_handler    → aws_iam_role.lambda_admin_handler.arn
#   sms_handler      → aws_iam_role.lambda_sms_handler.arn
#   invite_handler   → aws_iam_role.lambda_invite_handler.arn
#   reminder_handler → aws_iam_role.lambda_reminder_handler.arn
# =============================================================================

locals {
  log_actions = [
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
  ]
  members_arn       = aws_dynamodb_table.members.arn
  members_index     = "${aws_dynamodb_table.members.arn}/index/*"
  events_arn        = aws_dynamodb_table.events.arn
  invites_arn       = aws_dynamodb_table.event_invites.arn
  event_history_arn = aws_dynamodb_table.event_history.arn
  invites_index     = "${aws_dynamodb_table.event_invites.arn}/index/*"
  checkins_arn      = aws_dynamodb_table.checkins.arn
  audit_log_arn     = aws_dynamodb_table.audit_log.arn
  region            = data.aws_region.current.name
  account           = data.aws_caller_identity.current.account_id
}

# ── Shared assume-role policy ──────────────────────────────────────────────
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
        Action   = ["dynamodb:PutItem"]
        Resource = [local.events_arn]
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
          "dynamodb:BatchGetItem",
        ]
        Resource = [
          local.members_arn,
          local.members_index,
          local.events_arn,
          local.event_history_arn,
          local.invites_arn,
          local.invites_index,
          local.checkins_arn,
          local.audit_log_arn,
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
# Needs: members (read + opt-out write + scan for plus-one lookup),
#        invites (read + status write + query by phone-index),
#        events (read + pending approval write/delete),
#        Secrets Manager (QUO key + Claude API key + webhook secret)
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
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan", "dynamodb:Query"]
        Resource = [local.members_arn, local.members_index]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [local.invites_arn, local.invites_index]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = [local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.quo_api_key_secret_id}*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:rsvp/claude-api-key*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:${var.webhook_secret_id}*",
          "arn:aws:secretsmanager:${local.region}:${local.account}:secret:rsvp/webhook-secret-delivery*",
        ]
      },
    ]
  })
}

# =============================================================================
# invite_handler — blast invites to approved members
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
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan", "dynamodb:Query"]
        Resource = [local.members_arn, local.members_index]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [local.invites_arn, local.invites_index]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [local.audit_log_arn]
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
#
# Needs:
#   members  — BatchGetItem (batch member lookup), GetItem (individual fallback)
#   invites  — Query (get confirmed list), UpdateItem (claim/stamp sentinel fields)
#   events   — GetItem (load current event)
#   Secrets  — admin token (manual HTTP trigger), QUO key (send SMS)
#   audit    — PutItem/UpdateItem (log reminder blast)
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
        # BatchGetItem for batch member lookup; GetItem for individual fallback
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
        Resource = [local.members_arn]
      },
      {
        # Query to get confirmed invites; UpdateItem to claim/stamp reminder sentinels
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = [local.invites_arn, local.invites_index]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [local.events_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [local.audit_log_arn]
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
