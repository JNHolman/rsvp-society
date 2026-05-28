# =============================================================================
# audit_log.tf
#
# Immutable admin action audit trail.
# All admin actions write here via audit_log.py — best-effort, never blocking.
#
# Schema:
#   actionId   (S) — uuid, hash key
#   timestamp  (S) — ISO-8601, sort key on the GSI
#   action     (S) — ACTION_* constant (e.g. MEMBER_APPROVED)
#   actorToken (S) — last 8 chars of admin token
#   targetPhone (S) — member phone (when relevant)
#   targetName  (S) — truncated display name (when relevant)
#   metadata   (M) — action-specific extra fields
#   ttl        (N) — epoch seconds, auto-expires after 1 year
# =============================================================================

resource "aws_dynamodb_table" "audit_log" {
  name         = "rsvp-audit-log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "actionId"

  attribute {
    name = "actionId"
    type = "S"
  }

  # TTL — records auto-expire 1 year after creation.
  # Set `ttl` (epoch seconds) on every item written by audit_log.py.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # GSI — enables querying by actor or action type with time ordering
  # Supports future admin audit viewer: "show all actions by this admin"
  global_secondary_index {
    name            = "action-timestamp-index"
    hash_key        = "action"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  attribute {
    name = "action"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "rsvp-society"
    Environment = "prod"
  }
}

# =============================================================================
# IAM — grant PutItem on audit_log to the three functions that write to it.
# These are inline policy additions on the per-function roles from
# iam_per_function.tf. Each is a separate aws_iam_role_policy resource so
# they can be managed and reviewed independently.
# =============================================================================

resource "aws_iam_role_policy" "admin_handler_audit_log" {
  name = "admin-handler-audit-log-write"
  role = aws_iam_role.lambda_admin_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem"]
      Resource = aws_dynamodb_table.audit_log.arn
    }]
  })
}

resource "aws_iam_role_policy" "invite_handler_audit_log" {
  name = "invite-handler-audit-log-write"
  role = aws_iam_role.lambda_invite_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem"]
      Resource = aws_dynamodb_table.audit_log.arn
    }]
  })
}

resource "aws_iam_role_policy" "reminder_handler_audit_log" {
  name = "reminder-handler-audit-log-write"
  role = aws_iam_role.lambda_reminder_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem"]
      Resource = aws_dynamodb_table.audit_log.arn
    }]
  })
}

output "audit_log_table_name" {
  value       = aws_dynamodb_table.audit_log.name
  description = "AUDIT_LOG_TABLE_NAME env var — wired into admin_handler, invite_handler, and reminder_handler"
}
