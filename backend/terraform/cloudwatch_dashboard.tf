variable "alert_sms_phone" {
  description = "Optional SMS phone number for CloudWatch/SNS alerts. Leave blank to disable SMS alert subscription."
  type        = string
  default     = ""
}

resource "aws_cloudwatch_dashboard" "rsvp_operations" {
  dashboard_name = "rsvp-society-operations"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 1
        properties = {
          markdown = "# RSVP Society — Operations  |  us-east-1  |  prod"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 1
        width  = 12
        height = 6
        properties = {
          title   = "Lambda — Invocations"
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "Sum"
          region  = "us-east-1"
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", "rsvp-access-request", { label = "access_request" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "rsvp-sms-handler", { label = "sms_handler" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "rsvp-admin-handler", { label = "admin_handler" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "rsvp-reminder-handler", { label = "reminder_handler" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "rsvp-invite-handler", { label = "invite_handler" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 1
        width  = 12
        height = 6
        properties = {
          title   = "Lambda — Errors"
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "Sum"
          region  = "us-east-1"
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", "rsvp-access-request", { label = "access_request", color = "#d62728" }],
            ["AWS/Lambda", "Errors", "FunctionName", "rsvp-sms-handler", { label = "sms_handler", color = "#ff7f0e" }],
            ["AWS/Lambda", "Errors", "FunctionName", "rsvp-admin-handler", { label = "admin_handler", color = "#9467bd" }],
            ["AWS/Lambda", "Errors", "FunctionName", "rsvp-reminder-handler", { label = "reminder_handler", color = "#8c564b" }],
            ["AWS/Lambda", "Errors", "FunctionName", "rsvp-invite-handler", { label = "invite_handler", color = "#e377c2" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 7
        width  = 12
        height = 6
        properties = {
          title   = "Lambda — Duration p99 (ms)"
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "p99"
          region  = "us-east-1"
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", "rsvp-access-request", { label = "access_request" }],
            ["AWS/Lambda", "Duration", "FunctionName", "rsvp-sms-handler", { label = "sms_handler" }],
            ["AWS/Lambda", "Duration", "FunctionName", "rsvp-admin-handler", { label = "admin_handler" }],
            ["AWS/Lambda", "Duration", "FunctionName", "rsvp-reminder-handler", { label = "reminder_handler" }],
            ["AWS/Lambda", "Duration", "FunctionName", "rsvp-invite-handler", { label = "invite_handler" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 7
        width  = 12
        height = 6
        properties = {
          title   = "Lambda — Throttles"
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "Sum"
          region  = "us-east-1"
          metrics = [
            ["AWS/Lambda", "Throttles", "FunctionName", "rsvp-access-request", { label = "access_request", color = "#d62728" }],
            ["AWS/Lambda", "Throttles", "FunctionName", "rsvp-sms-handler", { label = "sms_handler", color = "#ff7f0e" }],
            ["AWS/Lambda", "Throttles", "FunctionName", "rsvp-admin-handler", { label = "admin_handler", color = "#9467bd" }],
            ["AWS/Lambda", "Throttles", "FunctionName", "rsvp-reminder-handler", { label = "reminder_handler", color = "#8c564b" }],
            ["AWS/Lambda", "Throttles", "FunctionName", "rsvp-invite-handler", { label = "invite_handler", color = "#e377c2" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 13
        width  = 8
        height = 6
        properties = {
          title  = "DynamoDB — Consumed Read Capacity"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "rsvp-members", { label = "members" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "rsvp-events", { label = "events" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "rsvp-event-invites", { label = "event-invites" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "rsvp-checkins", { label = "checkins" }],
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "rsvp-audit-log", { label = "audit-log" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 13
        width  = 8
        height = 6
        properties = {
          title  = "DynamoDB — Consumed Write Capacity"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "rsvp-members", { label = "members" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "rsvp-events", { label = "events" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "rsvp-event-invites", { label = "event-invites" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "rsvp-checkins", { label = "checkins" }],
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", "rsvp-audit-log", { label = "audit-log" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 13
        width  = 8
        height = 6
        properties = {
          title  = "DynamoDB — System Errors"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/DynamoDB", "SystemErrors", "TableName", "rsvp-members", "Operation", "GetItem", { label = "members GetItem", color = "#d62728" }],
            ["AWS/DynamoDB", "SystemErrors", "TableName", "rsvp-members", "Operation", "PutItem", { label = "members PutItem", color = "#ff7f0e" }],
            ["AWS/DynamoDB", "SystemErrors", "TableName", "rsvp-event-invites", "Operation", "Query", { label = "invites Query", color = "#9467bd" }],
            ["AWS/DynamoDB", "SystemErrors", "TableName", "rsvp-checkins", "Operation", "PutItem", { label = "checkins PutItem", color = "#e377c2" }],
            ["AWS/DynamoDB", "SystemErrors", "TableName", "rsvp-audit-log", "Operation", "PutItem", { label = "audit-log PutItem", color = "#bcbd22" }],
            ["AWS/DynamoDB", "SystemErrors", "TableName", "rsvp-events", "Operation", "UpdateItem", { label = "events UpdateItem", color = "#8c564b" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 19
        width  = 8
        height = 6
        properties = {
          title  = "API Gateway — Request Count"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiName", "rsvp-api", "Stage", "prod", { label = "Total Requests" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 19
        width  = 8
        height = 6
        properties = {
          title  = "API Gateway — 4xx Errors"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/ApiGateway", "4XXError", "ApiName", "rsvp-api", "Stage", "prod", { label = "4xx", color = "#ff7f0e" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 19
        width  = 8
        height = 6
        properties = {
          title  = "API Gateway — 5xx Errors"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/ApiGateway", "5XXError", "ApiName", "rsvp-api", "Stage", "prod", { label = "5xx", color = "#d62728" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 25
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway — Latency p99 (ms)"
          view   = "timeSeries"
          period = 300
          stat   = "p99"
          region = "us-east-1"
          metrics = [
            ["AWS/ApiGateway", "Latency", "ApiName", "rsvp-api", "Stage", "prod", { label = "End-to-end p99" }],
            ["AWS/ApiGateway", "IntegrationLatency", "ApiName", "rsvp-api", "Stage", "prod", { label = "Integration p99" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 25
        width  = 12
        height = 6
        properties = {
          title  = "WAF — Blocked Requests"
          view   = "timeSeries"
          period = 300
          stat   = "Sum"
          region = "us-east-1"
          metrics = [
            ["AWS/WAFV2", "BlockedRequests", "WebACL", "rsvp-api-acl", "Region", "us-east-1", "Rule", "rate-limit-access", { label = "Blocked", color = "#d62728" }]
          ]
          yAxis = { left = { min = 0 } }
        }
      }
    ]
  })
}

resource "aws_sns_topic" "rsvp_alerts" {
  name = "rsvp-society-alerts"
}

# Email alerts → info@rsvpsociety.com
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.rsvp_alerts.arn
  protocol  = "email"
  endpoint  = "info@rsvpsociety.com"
}

# SMS alerts → host phone
resource "aws_sns_topic_subscription" "alerts_sms" {
  count     = var.alert_sms_phone != "" ? 1 : 0
  topic_arn = aws_sns_topic.rsvp_alerts.arn
  protocol  = "sms"
  endpoint  = var.alert_sms_phone
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = toset([
    "rsvp-access-request",
    "rsvp-sms-handler",
    "rsvp-admin-handler",
    "rsvp-reminder-handler",
    "rsvp-invite-handler",
  ])

  alarm_name          = "rsvp-lambda-errors-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Lambda ${each.key} errors exceeded 5 in 10 min"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = each.key
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
  ok_actions    = [aws_sns_topic.rsvp_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "rsvp-api-5xx-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "5XXError"
  namespace           = "AWS/ApiGateway"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "API Gateway 5xx errors exceeded 10 in 10 min"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiName = "rsvp-api"
    Stage   = "prod"
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
  ok_actions    = [aws_sns_topic.rsvp_alerts.arn]
}

# =============================================================================
# Additional alarms — throttles, latency, DynamoDB, reminder pipeline
# =============================================================================

# Lambda throttles — any throttle on any function is actionable
resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each = toset([
    "rsvp-access-request",
    "rsvp-sms-handler",
    "rsvp-admin-handler",
    "rsvp-reminder-handler",
    "rsvp-invite-handler",
  ])

  alarm_name          = "rsvp-lambda-throttles-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Lambda ${each.key} is being throttled"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = each.key
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
  ok_actions    = [aws_sns_topic.rsvp_alerts.arn]
}

# Lambda p99 latency — alert when any function runs slow (invite blast excluded,
# it legitimately runs long during large waves)
resource "aws_cloudwatch_metric_alarm" "lambda_latency" {
  for_each = toset([
    "rsvp-access-request",
    "rsvp-sms-handler",
    "rsvp-admin-handler",
    "rsvp-reminder-handler",
  ])

  alarm_name          = "rsvp-lambda-latency-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 300
  extended_statistic  = "p99"
  threshold           = 5000
  alarm_description   = "Lambda ${each.key} p99 latency exceeded 5s"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = each.key
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
}

# DynamoDB system errors — covers members, events, invites, checkins, audit-log
resource "aws_cloudwatch_metric_alarm" "dynamodb_errors" {
  for_each = toset([
    "rsvp-members",
    "rsvp-events",
    "rsvp-event-invites",
    "rsvp-checkins",
    "rsvp-audit-log"
  ])

  alarm_name          = "rsvp-dynamodb-errors-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "SystemErrors"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "DynamoDB table ${each.key} system errors exceeded 5 in 10 min"
  treat_missing_data  = "notBreaching"

  dimensions = {
    TableName = each.key
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
  ok_actions    = [aws_sns_topic.rsvp_alerts.arn]
}

# DynamoDB throttled requests — PAY_PER_REQUEST tables shouldn't throttle,
# but burst capacity can still be exceeded
resource "aws_cloudwatch_metric_alarm" "dynamodb_throttles" {
  for_each = toset([
    "rsvp-members",
    "rsvp-event-invites"
  ])

  alarm_name          = "rsvp-dynamodb-throttles-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ThrottledRequests"
  namespace           = "AWS/DynamoDB"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "DynamoDB table ${each.key} is being throttled"
  treat_missing_data  = "notBreaching"

  dimensions = {
    TableName = each.key
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
}

# Reminder pipeline — alarm if reminder Lambda errors during the two
# scheduled windows (6 PM day-before, 4 PM day-of). Uses the same
# lambda_errors for_each but with a tighter threshold since reminder
# failures are high-visibility business impact.
resource "aws_cloudwatch_metric_alarm" "reminder_errors" {
  alarm_name          = "rsvp-reminder-handler-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "rsvp-reminder-handler errored — reminder SMS may not have gone out"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = "rsvp-reminder-handler"
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
  ok_actions    = [aws_sns_topic.rsvp_alerts.arn]
}

# Signature failures deliberately return HTTP 200 to Quo, so the API 4xx/5xx
# alarms cannot see these rejected webhooks. Count the handler's explicit log.
resource "aws_cloudwatch_log_metric_filter" "invalid_webhook_signature" {
  name           = "rsvp-invalid-webhook-signature"
  log_group_name = aws_cloudwatch_log_group.sms_handler.name
  pattern        = "\"rejected request with invalid webhook signature\""

  metric_transformation {
    name      = "InvalidWebhookSignature"
    namespace = "RSVPSociety/Security"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "invalid_webhook_signature" {
  alarm_name          = "rsvp-invalid-webhook-signature"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.invalid_webhook_signature.metric_transformation[0].name
  namespace           = aws_cloudwatch_log_metric_filter.invalid_webhook_signature.metric_transformation[0].namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "SMS webhook requests failed signature verification"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
}

# API Gateway latency p99 — catch slow backend responses before users notice
resource "aws_cloudwatch_metric_alarm" "api_latency" {
  alarm_name          = "rsvp-api-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "Latency"
  namespace           = "AWS/ApiGateway"
  period              = 300
  extended_statistic  = "p99"
  threshold           = 3000
  alarm_description   = "API Gateway p99 latency exceeded 3s"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiName = "rsvp-api"
    Stage   = "prod"
  }

  alarm_actions = [aws_sns_topic.rsvp_alerts.arn]
}

# =============================================================================


# One failed invite worker matters: it may interrupt the remaining recipients.
resource "aws_cloudwatch_metric_alarm" "invite_worker_failure" {
  for_each            = toset(["Errors", "AsyncEventsDropped"])
  alarm_name          = "rsvp-invite-worker-${each.key}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = each.key
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_description   = "Invite worker failed or expired; inspect the job and delivery records before resending."
  dimensions          = { FunctionName = aws_lambda_function.invite_handler.function_name }
  alarm_actions       = [aws_sns_topic.rsvp_alerts.arn]
}

# Application failures are caught and stored as FAILED jobs, so count their log too.
resource "aws_cloudwatch_log_metric_filter" "invite_job_failed" {
  name           = "rsvp-invite-job-failed"
  log_group_name = aws_cloudwatch_log_group.invite_handler.name
  pattern        = "\"invite_job_failed\""
  metric_transformation {
    name      = "InviteJobFailed"
    namespace = "RSVPSociety/Operations"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "invite_job_failed" {
  alarm_name          = "rsvp-invite-job-failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "InviteJobFailed"
  namespace           = "RSVPSociety/Operations"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_description   = "Invite job failed; review its error and delivered recipients before resending."
  alarm_actions       = [aws_sns_topic.rsvp_alerts.arn]
}

resource "aws_cloudwatch_log_metric_filter" "auto_wave_failed" {
  name           = "rsvp-auto-wave-failed"
  log_group_name = aws_cloudwatch_log_group.invite_handler.name
  pattern        = "\"auto_wave_failed\""
  metric_transformation {
    name      = "AutoWaveFailed"
    namespace = "RSVPSociety/Operations"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "auto_wave_failed" {
  alarm_name          = "rsvp-auto-wave-failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "AutoWaveFailed"
  namespace           = "RSVPSociety/Operations"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_description   = "Automatic invite-wave scheduling failed; inspect the completed send job and active-event details."
  alarm_actions       = [aws_sns_topic.rsvp_alerts.arn]
}
