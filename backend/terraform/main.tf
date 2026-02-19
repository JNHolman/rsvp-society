terraform {
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

# ── IAM Role for Lambda ───────────────────────────────────
resource "aws_iam_role" "lambda_role" {
  name = "rsvp-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "rsvp-lambda-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:us-east-1:852121054175:secret:rsvp/superphone-api-key*",
          "arn:aws:secretsmanager:us-east-1:852121054175:secret:rsvp/claude-api-key*"
        ]
      }
    ]
  })
}

# ── Lambda Layer for Anthropic SDK ───────────────────────
resource "aws_lambda_layer_version" "anthropic" {
  filename            = "${path.module}/anthropic_layer.zip"
  layer_name          = "anthropic-sdk"
  compatible_runtimes = ["python3.12"]
  description         = "Anthropic Python SDK"
}

# ── Access Request Lambda ─────────────────────────────────
data "archive_file" "access_request" {
  type        = "zip"
  source_file = "${path.module}/../lambda/access_request.py"
  output_path = "${path.module}/access_request.zip"
}

resource "aws_lambda_function" "access_request" {
  filename         = data.archive_file.access_request.output_path
  function_name    = "rsvp-access-request"
  role             = aws_iam_role.lambda_role.arn
  handler          = "access_request.handler"
  runtime          = "python3.12"
  timeout          = 30
  source_code_hash = data.archive_file.access_request.output_base64sha256

  environment {
    variables = {
      ENVIRONMENT = "production"
    }
  }
}

# ── SMS Handler Lambda ────────────────────────────────────
data "archive_file" "sms_handler" {
  type        = "zip"
  source_file = "${path.module}/../lambda/sms_handler.py"
  output_path = "${path.module}/sms_handler.zip"
}

resource "aws_lambda_function" "sms_handler" {
  filename         = data.archive_file.sms_handler.output_path
  function_name    = "rsvp-sms-handler"
  role             = aws_iam_role.lambda_role.arn
  handler          = "sms_handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  source_code_hash = data.archive_file.sms_handler.output_base64sha256
  layers           = [aws_lambda_layer_version.anthropic.arn]

  environment {
    variables = {
      ENVIRONMENT = "production"
    }
  }
}

# ── API Gateway ───────────────────────────────────────────
resource "aws_apigatewayv2_api" "rsvp_api" {
  name          = "rsvp-society-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["https://rsvpsociety.com"]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["Content-Type"]
  }
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.rsvp_api.id
  name        = "prod"
  auto_deploy = true
}

# Access Request endpoint
resource "aws_apigatewayv2_integration" "access_request" {
  api_id                 = aws_apigatewayv2_api.rsvp_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.access_request.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "access_request" {
  api_id    = aws_apigatewayv2_api.rsvp_api.id
  route_key = "POST /access"
  target    = "integrations/${aws_apigatewayv2_integration.access_request.id}"
}

# SMS Handler endpoint
resource "aws_apigatewayv2_integration" "sms_handler" {
  api_id                 = aws_apigatewayv2_api.rsvp_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.sms_handler.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "sms_handler" {
  api_id    = aws_apigatewayv2_api.rsvp_api.id
  route_key = "POST /sms"
  target    = "integrations/${aws_apigatewayv2_integration.sms_handler.id}"
}

# Lambda permissions for API Gateway
resource "aws_lambda_permission" "access_request" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.access_request.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.rsvp_api.execution_arn}/*/*"
}

resource "aws_lambda_permission" "sms_handler" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sms_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.rsvp_api.execution_arn}/*/*"
}

# ── Outputs ───────────────────────────────────────────────
output "access_request_url" {
  value       = "${aws_apigatewayv2_stage.prod.invoke_url}/access"
  description = "Paste this into index.html form endpoint"
}

output "sms_webhook_url" {
  value       = "${aws_apigatewayv2_stage.prod.invoke_url}/sms"
  description = "Paste this into Superphone webhook settings"
}
