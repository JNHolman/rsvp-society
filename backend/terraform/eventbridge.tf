# -----------------------------
# Lambda — Reminder Handler
# -----------------------------
resource "aws_lambda_function" "reminder_handler" {
  function_name = "rsvp-reminder-handler"
  role          = aws_iam_role.lambda_reminder_handler.arn
  handler       = "reminder_handler.handler"
  runtime       = "python3.11"
  timeout       = 300 # rate-limited sends to 300+ confirmed members
  filename      = data.archive_file.lambda_bundle.output_path

  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  environment {
    variables = {
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME    = aws_dynamodb_table.event_invites.name
      MEMBERS_TABLE_NAME    = aws_dynamodb_table.members.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      ADMIN_TOKEN_SECRET_ID = var.admin_token_secret_id
      AUDIT_LOG_TABLE_NAME  = aws_dynamodb_table.audit_log.name
      SMS_PROVIDER          = "quo"
      QUO_API_KEY_SECRET_ID = var.quo_api_key_secret_id
      QUO_PHONE_NUMBER_ID   = var.quo_phone_number_id
      SMS_ENABLED           = "true"
    }
  }
}

# API Gateway resource for manual blast
resource "aws_api_gateway_resource" "admin_invite_reminder" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_invite.id
  path_part   = "reminder"
}

resource "aws_api_gateway_method" "admin_invite_reminder_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_reminder.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_invite_reminder_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_invite_reminder.id
  http_method             = aws_api_gateway_method.admin_invite_reminder_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.reminder_handler.invoke_arn
}

resource "aws_lambda_permission" "reminder_api" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reminder_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

# CORS OPTIONS for reminder endpoint
resource "aws_api_gateway_method" "admin_invite_reminder_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_reminder.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_reminder_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_invite_reminder.id
  http_method       = aws_api_gateway_method.admin_invite_reminder_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_invite_reminder_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_reminder.id
  http_method = aws_api_gateway_method.admin_invite_reminder_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_invite_reminder_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_reminder.id
  http_method = aws_api_gateway_method.admin_invite_reminder_options.http_method
  status_code = aws_api_gateway_method_response.admin_invite_reminder_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

# -----------------------------
# EventBridge — Hourly reminder check
# Two separate hourly rules pass timing context to the Lambda.
# The Lambda now decides whether it is the correct local event date/hour
# using the saved event_timezone (default America/New_York).
# -----------------------------
resource "aws_cloudwatch_event_rule" "reminder_day_before" {
  name                = "rsvp-reminder-day-before"
  description         = "Every 5 min all day — Lambda checks event timezone and fires at correct local time"
  schedule_expression = "cron(0/5 * * * ? *)"
}

resource "aws_cloudwatch_event_rule" "reminder_day_of" {
  name                = "rsvp-reminder-day-of"
  description         = "Every 5 min all day — Lambda checks event timezone and fires at correct local time"
  schedule_expression = "cron(0/5 * * * ? *)"
}

resource "aws_cloudwatch_event_target" "reminder_day_before" {
  rule      = aws_cloudwatch_event_rule.reminder_day_before.name
  target_id = "ReminderDayBefore"
  arn       = aws_lambda_function.reminder_handler.arn
  input     = jsonencode({ source = "eventbridge", timing = "day_before" })
}

resource "aws_cloudwatch_event_target" "reminder_day_of" {
  rule      = aws_cloudwatch_event_rule.reminder_day_of.name
  target_id = "ReminderDayOf"
  arn       = aws_lambda_function.reminder_handler.arn
  input     = jsonencode({ source = "eventbridge", timing = "day_of" })
}

resource "aws_lambda_permission" "reminder_eventbridge_day_before" {
  statement_id  = "AllowEventBridgeDayBefore"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reminder_handler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reminder_day_before.arn
}

resource "aws_lambda_permission" "reminder_eventbridge_day_of" {
  statement_id  = "AllowEventBridgeDayOf"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reminder_handler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reminder_day_of.arn
}
