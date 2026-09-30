# =============================================================================
# invite_jobs.tf
# Dedicated invite job records for admin send-status polling.
# =============================================================================

resource "aws_dynamodb_table" "invite_jobs" {
  name         = "rsvp-invite-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "jobId"

  attribute {
    name = "jobId"
    type = "S"
  }

  attribute {
    name = "eventId"
    type = "S"
  }

  attribute {
    name = "submittedAt"
    type = "S"
  }

  global_secondary_index {
    name            = "eventId-submittedAt-index"
    hash_key        = "eventId"
    range_key       = "submittedAt"
    projection_type = "ALL"
  }

  # Preview locks and job status rows are operational records, not permanent
  # event/member history. Lambda writes `ttl` epoch seconds.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

output "invite_jobs_table_name" {
  value = aws_dynamodb_table.invite_jobs.name
}

# A timeout can leave a provider outcome unknown. Do not replay the chunk
# automatically; alert the operator to inspect delivery records first.
resource "aws_lambda_function_event_invoke_config" "invite_handler" {
  function_name                = aws_lambda_function.invite_handler.function_name
  maximum_event_age_in_seconds = 3600
  maximum_retry_attempts       = 0
}
