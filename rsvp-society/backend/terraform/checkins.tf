# ─────────────────────────────────────────────────────────────────────────────
# checkins.tf
# Per-event check-in deduplication table.
# Composite key: eventId (hash) + phone (range).
# A ConditionExpression on put_item guarantees idempotent check-ins —
# repeated taps on the door iPad never double-count attendance.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "checkins" {
  name         = "rsvp-checkins"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "eventId"
  range_key    = "phone"

  attribute {
    name = "eventId"
    type = "S"
  }

  attribute {
    name = "phone"
    type = "S"
  }

  # Auto-expire records 90 days after the event to keep the table lean.
  # Set checkins item attribute `ttl` (epoch seconds) when writing.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "rsvp-society"
    Environment = "prod"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# IAM: checkins access is granted via iam_per_function.tf.
# The admin_handler inline policy includes dynamodb:PutItem on this table ARN.
# No separate policy or attachment needed here.
# ─────────────────────────────────────────────────────────────────────────────

output "checkins_table_name" {
  value       = aws_dynamodb_table.checkins.name
  description = "CHECKINS_TABLE_NAME env var — already wired into admin_handler in main.tf"
}
