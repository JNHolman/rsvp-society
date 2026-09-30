# -----------------------------
# Lambda — Reminder Handler
# -----------------------------
resource "aws_lambda_function" "reminder_handler" {
  function_name = "rsvp-reminder-handler"
  role          = aws_iam_role.lambda_reminder_handler.arn
  handler       = "reminder_handler.handler"
  runtime       = "python3.13"
  timeout       = 300 # reminder sends are capped at 15 per invocation for provider timeout margin
  filename      = local.lambda_bundle_path

  source_code_hash = filebase64sha256(local.lambda_bundle_path)

  environment {
    variables = {
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME    = aws_dynamodb_table.event_invites.name
      INVITE_JOBS_TABLE_NAME = aws_dynamodb_table.invite_jobs.name
      MEMBERS_TABLE_NAME    = aws_dynamodb_table.members.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      ADMIN_TOKEN_SECRET_ID = var.admin_token_secret_id
      AUDIT_LOG_TABLE_NAME  = aws_dynamodb_table.audit_log.name
      SMS_PROVIDER          = "quo"
      QUO_API_KEY_SECRET_ID = var.quo_api_key_secret_id
      QUO_API_BASE_URL     = var.quo_api_base_url
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
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/POST/admin/invite/reminder"
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
# EventBridge Scheduler — one-time reminder delivery
# The admin Lambda creates at most two one-time schedules for the active event
# (day-before/day-of). Each schedule auto-deletes after it fires.
# -----------------------------
resource "aws_iam_role" "reminder_scheduler_invoker" {
  name = "rsvp-reminder-scheduler-invoker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "reminder_scheduler_invoker" {
  name = "invoke-reminder-lambda"
  role = aws_iam_role.reminder_scheduler_invoker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.reminder_handler.arn
    }]
  })
}
