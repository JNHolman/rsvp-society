# -----------------------------
# Lambda — Reminder Handler
# -----------------------------
resource "aws_lambda_function" "reminder_handler" {
  function_name = "rsvp-reminder-handler"
  role          = aws_iam_role.lambda_role.arn
  handler       = "reminder_handler.handler"
  runtime       = "python3.11"
  timeout       = 60
  filename         = data.archive_file.lambda_bundle.output_path

  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  environment {
    variables = {
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME    = aws_dynamodb_table.event_invites.name
      MEMBERS_TABLE_NAME    = aws_dynamodb_table.members.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      ADMIN_TOKEN_SECRET_ID = var.admin_token_secret_id
      SMS_PROVIDER          = "quo"
      QUO_API_KEY_SECRET_ID = var.quo_api_key_secret_id
      SEND_WELCOME_SMS      = "false"
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
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_reminder.id
  http_method = aws_api_gateway_method.admin_invite_reminder_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
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
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

# -----------------------------
# EventBridge — Daily reminder check
# Fires at 6PM EST (23:00 UTC) for day-before reminders
# and 4PM EST (21:00 UTC) for day-of reminders
# Lambda checks which applies based on event settings
# -----------------------------
resource "aws_cloudwatch_event_rule" "reminder_day_before" {
  name                = "rsvp-reminder-day-before"
  description         = "Fires daily at 6PM EST to send day-before reminders"
  schedule_expression = "cron(0 23 * * ? *)"
}

resource "aws_cloudwatch_event_rule" "reminder_day_of" {
  name                = "rsvp-reminder-day-of"
  description         = "Fires daily at 4PM EST to send day-of reminders"
  schedule_expression = "cron(0 21 * * ? *)"
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
