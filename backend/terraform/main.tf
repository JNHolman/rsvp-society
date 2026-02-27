terraform {
  backend "s3" {
    bucket         = "rsvp-society-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "rsvp-terraform-locks"
    encrypt        = true
  }
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

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

variable "members_table_name" {
  type    = string
  default = "rsvp-members"
}

variable "allowed_origins" {
  type    = list(string)
  default = [
  "https://rsvpsociety.com",
  "https://www.rsvpsociety.com",
  "http://localhost:8000"]
}

variable "quo_api_key_secret_id" {
  type    = string
  default = "rsvp/quo-api-key"
}

variable "admin_token_secret_id" {
  type    = string
  default = "rsvp/admin-token"
}

locals {
  allowed_origins_csv = join(",", var.allowed_origins)
  cors_origins        = "'https://rsvpsociety.com'"
  cors_methods        = "'GET,POST,PUT,DELETE,OPTIONS'"
  cors_headers        = "'content-type,x-admin-token'"
}

# -----------------------------
# DynamoDB (source of truth)
# -----------------------------
resource "aws_dynamodb_table" "members" {
  name         = var.members_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "phone"

  attribute {
    name = "phone"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}


# -----------------------------
# DynamoDB — Events (current event source of truth)
# -----------------------------
resource "aws_dynamodb_table" "events" {
  name         = "rsvp-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "eventId"

  attribute {
    name = "eventId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# -----------------------------
# DynamoDB — EventInvites (composite key: eventId + phone)
# -----------------------------
resource "aws_dynamodb_table" "event_invites" {
  name         = "rsvp-event-invites"
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

  # GSI: look up all events a member was invited to (phone → eventId)
  global_secondary_index {
    name            = "phone-index"
    hash_key        = "phone"
    range_key       = "eventId"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# -----------------------------
# IAM (Lambda execution)
# -----------------------------
resource "aws_iam_role" "lambda_role" {
  name = "rsvp-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_policy" "lambda_policy" {
  name = "rsvp-lambda-policy"

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
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DeleteItem"
        ]
        Resource = [
          aws_dynamodb_table.members.arn,
          aws_dynamodb_table.events.arn,
          aws_dynamodb_table.event_invites.arn,
          "${aws_dynamodb_table.event_invites.arn}/index/*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:${var.quo_api_key_secret_id}*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:${var.admin_token_secret_id}*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:rsvp/claude-api-key*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_policy_attach" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

# -----------------------------
# Lambda packaging (one bundle)
# -----------------------------
data "archive_file" "lambda_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/lambda_bundle.zip"
}

resource "aws_lambda_function" "access_request" {
  function_name = "rsvp-access-request"
  role          = aws_iam_role.lambda_role.arn
  handler       = "access_request.handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_bundle.output_path
  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  timeout = 10

  environment {
    variables = {
      ENVIRONMENT           = "prod"
      MEMBERS_TABLE_NAME    = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME    = aws_dynamodb_table.event_invites.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      SMS_ENABLED           = "false"
      SMS_PROVIDER          = "quo"
      QUO_API_KEY_SECRET_ID = var.quo_api_key_secret_id
    }
  }
}

resource "aws_lambda_function" "admin_handler" {
  function_name = "rsvp-admin-handler"
  role          = aws_iam_role.lambda_role.arn
  handler       = "admin_handler.handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_bundle.output_path
  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  timeout = 60  # increased for bulk import

  environment {
    variables = {
      ENVIRONMENT           = "prod"
      MEMBERS_TABLE_NAME    = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME    = aws_dynamodb_table.event_invites.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      ADMIN_TOKEN_SECRET_ID = var.admin_token_secret_id
    }
  }
}

resource "aws_lambda_function" "sms_handler" {
  function_name = "rsvp-sms-handler"
  role          = aws_iam_role.lambda_role.arn
  handler       = "sms_handler.handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_bundle.output_path
  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  timeout = 15

  environment {
    variables = {
      ENVIRONMENT              = "prod"
      MEMBERS_TABLE_NAME       = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME        = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME       = aws_dynamodb_table.event_invites.name
      ALLOWED_ORIGINS          = local.allowed_origins_csv
      SMS_PROVIDER             = "quo"
      QUO_API_KEY_SECRET_ID    = var.quo_api_key_secret_id
      CLAUDE_API_KEY_SECRET_ID = "rsvp/claude-api-key"
    }
  }
}



resource "aws_lambda_function" "event_handler" {
  function_name = "rsvp-event-handler"
  role          = aws_iam_role.lambda_role.arn
  handler       = "event_handler.handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_bundle.output_path
  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  timeout = 15

  environment {
    variables = {
      ENVIRONMENT           = "prod"
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      ADMIN_TOKEN_SECRET_ID = var.admin_token_secret_id
    }
  }
}

resource "aws_lambda_function" "invite_handler" {
  function_name = "rsvp-invite-handler"
  role          = aws_iam_role.lambda_role.arn
  handler       = "invite_handler.handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_bundle.output_path
  source_code_hash = data.archive_file.lambda_bundle.output_base64sha256

  timeout = 60  # blast can take longer for large lists

  environment {
    variables = {
      ENVIRONMENT           = "prod"
      MEMBERS_TABLE_NAME    = aws_dynamodb_table.members.name
      EVENTS_TABLE_NAME     = aws_dynamodb_table.events.name
      INVITES_TABLE_NAME    = aws_dynamodb_table.event_invites.name
      ALLOWED_ORIGINS       = local.allowed_origins_csv
      SMS_ENABLED           = "false"
      SMS_PROVIDER          = "quo"
      QUO_API_KEY_SECRET_ID = var.quo_api_key_secret_id
      ADMIN_TOKEN_SECRET_ID = var.admin_token_secret_id
    }
  }
}

# -----------------------------
# API Gateway REST API (v1)
# -----------------------------
resource "aws_api_gateway_rest_api" "api" {
  name = "rsvp-api"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# Resources
resource "aws_api_gateway_resource" "access" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "access"
}

resource "aws_api_gateway_resource" "admin" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "admin"
}

resource "aws_api_gateway_resource" "admin_members" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "members"
}

resource "aws_api_gateway_resource" "admin_members_status" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "status"
}

resource "aws_api_gateway_resource" "sms" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "sms"
}

resource "aws_api_gateway_resource" "sms_inbound" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.sms.id
  path_part   = "inbound"
}

# -----------------------------
# Methods + Integrations (Lambda Proxy)
# -----------------------------
# /access POST
resource "aws_api_gateway_method" "access_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.access.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "access_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.access.id
  http_method             = aws_api_gateway_method.access_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.access_request.invoke_arn
}

# /admin/members GET
resource "aws_api_gateway_method" "admin_members_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members.id
  http_method             = aws_api_gateway_method.admin_members_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members DELETE
resource "aws_api_gateway_method" "admin_members_delete" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members.id
  http_method   = "DELETE"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_delete" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members.id
  http_method             = aws_api_gateway_method.admin_members_delete.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/status POST
resource "aws_api_gateway_method" "admin_members_status_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_status.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_status_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_status.id
  http_method             = aws_api_gateway_method.admin_members_status_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /sms/inbound POST (kept for later)
resource "aws_api_gateway_method" "sms_inbound_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.sms_inbound.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "sms_inbound_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.sms_inbound.id
  http_method             = aws_api_gateway_method.sms_inbound_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.sms_handler.invoke_arn
}



# /event
resource "aws_api_gateway_resource" "event" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "event"
}

# /event/current (public)
resource "aws_api_gateway_resource" "event_current" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.event.id
  path_part   = "current"
}

resource "aws_api_gateway_method" "event_current_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event_current.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_current_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.event_current.id
  http_method             = aws_api_gateway_method.event_current_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.event_handler.invoke_arn
}

# /admin/event
resource "aws_api_gateway_resource" "admin_event" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "event"
}

resource "aws_api_gateway_method" "admin_event_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event.id
  http_method             = aws_api_gateway_method.admin_event_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.event_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_event_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event.id
  http_method             = aws_api_gateway_method.admin_event_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.event_handler.invoke_arn
}

# /admin/invite
resource "aws_api_gateway_resource" "admin_invite" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "invite"
}

# /admin/invite/preview
resource "aws_api_gateway_resource" "admin_invite_preview" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_invite.id
  path_part   = "preview"
}

# /admin/invite/send
resource "aws_api_gateway_resource" "admin_invite_send" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_invite.id
  path_part   = "send"
}

# /admin/members/gender
resource "aws_api_gateway_resource" "admin_members_gender" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "gender"
}

# /admin/members/tier
resource "aws_api_gateway_resource" "admin_members_tier" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "tier"
}

# /admin/members/attendance
resource "aws_api_gateway_resource" "admin_members_attendance" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "attendance"
}

# /admin/members/import
resource "aws_api_gateway_resource" "admin_members_import" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "import"
}

# Methods + integrations
resource "aws_api_gateway_method" "admin_invite_preview_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_preview.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_preview_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_invite_preview.id
  http_method             = aws_api_gateway_method.admin_invite_preview_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.invite_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_invite_send_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_send.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_invite_send_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_invite_send.id
  http_method             = aws_api_gateway_method.admin_invite_send_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.invite_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_gender_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_gender.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_gender_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_gender.id
  http_method             = aws_api_gateway_method.admin_members_gender_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_tier_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_tier.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_tier_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_tier.id
  http_method             = aws_api_gateway_method.admin_members_tier_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_attendance_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_attendance.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_attendance_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_attendance.id
  http_method             = aws_api_gateway_method.admin_members_attendance_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/import POST
resource "aws_api_gateway_method" "admin_members_import_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_import.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_import_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_import.id
  http_method             = aws_api_gateway_method.admin_members_import_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/import OPTIONS (CORS preflight)
resource "aws_api_gateway_method" "admin_members_import_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_import.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_import_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_import.id
  http_method = aws_api_gateway_method.admin_members_import_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}
resource "aws_api_gateway_method_response" "admin_members_import_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_import.id
  http_method = aws_api_gateway_method.admin_members_import_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_import_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_import.id
  http_method = aws_api_gateway_method.admin_members_import_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_import_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.allowed_origins[0]}'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
  }
  depends_on = [
    aws_api_gateway_integration.admin_members_import_options,
    aws_api_gateway_method_response.admin_members_import_options_200,
  ]
}



# /admin/members/search
resource "aws_api_gateway_resource" "admin_members_search" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "search"
}

# /admin/members/search GET
resource "aws_api_gateway_method" "admin_members_search_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_search.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_search_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_search.id
  http_method             = aws_api_gateway_method.admin_members_search_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# /admin/members/search OPTIONS (CORS preflight)
resource "aws_api_gateway_method" "admin_members_search_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_search.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_search_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_search.id
  http_method = aws_api_gateway_method.admin_members_search_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "admin_members_search_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_search.id
  http_method = aws_api_gateway_method.admin_members_search_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_search_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_search.id
  http_method = aws_api_gateway_method.admin_members_search_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_search_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.allowed_origins[0]}'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
  }
  depends_on = [
    aws_api_gateway_integration.admin_members_search_options,
    aws_api_gateway_method_response.admin_members_search_options_200,
  ]
}

# /event (public — no auth)
resource "aws_api_gateway_resource" "event_public" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "event"
}

resource "aws_api_gateway_method" "event_public_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event_public.id
  http_method   = "GET"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_public_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.event_public.id
  http_method             = aws_api_gateway_method.event_public_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# -----------------------------
# CORS (OPTIONS) - required for browser calls
# -----------------------------
# Helper: we set method response headers, then mock integration returns them.

# /access OPTIONS
resource "aws_api_gateway_method" "access_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.access.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "access_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.access.id
  http_method = aws_api_gateway_method.access_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "access_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.access.id
  http_method = aws_api_gateway_method.access_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "access_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.access.id
  http_method = aws_api_gateway_method.access_options.http_method
  status_code = aws_api_gateway_method_response.access_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.allowed_origins[0]}'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type'"
  }
}

# /admin/members OPTIONS
resource "aws_api_gateway_method" "admin_members_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members.id
  http_method = aws_api_gateway_method.admin_members_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "admin_members_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members.id
  http_method = aws_api_gateway_method.admin_members_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members.id
  http_method = aws_api_gateway_method.admin_members_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.allowed_origins[0]}'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
  }
}

# /admin/members/status OPTIONS
resource "aws_api_gateway_method" "admin_members_status_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_status.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_status_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_status.id
  http_method = aws_api_gateway_method.admin_members_status_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "admin_members_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_status.id
  http_method = aws_api_gateway_method.admin_members_status_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_members_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_status.id
  http_method = aws_api_gateway_method.admin_members_status_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_status_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.allowed_origins[0]}'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
  }
}

# /sms/inbound OPTIONS (for later)
resource "aws_api_gateway_method" "sms_inbound_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.sms_inbound.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "sms_inbound_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.sms_inbound.id
  http_method = aws_api_gateway_method.sms_inbound_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "sms_inbound_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.sms_inbound.id
  http_method = aws_api_gateway_method.sms_inbound_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "sms_inbound_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.sms_inbound.id
  http_method = aws_api_gateway_method.sms_inbound_options.http_method
  status_code = aws_api_gateway_method_response.sms_inbound_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.allowed_origins[0]}'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type'"
  }
}

# -----------------------------
# Deployment + Stage
# -----------------------------
resource "aws_api_gateway_deployment" "deploy" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeploy = sha1(join("", [
    filesha1("${path.module}/main.tf"),
    filesha1("${path.module}/cloudwatch_dashboard.tf"),
    filesha1("${path.module}/eventbridge.tf")
  ]))
  }
  depends_on = [
    aws_api_gateway_integration.access_post,
    aws_api_gateway_integration.admin_members_get,
    aws_api_gateway_integration.admin_members_status_post,
    aws_api_gateway_integration.sms_inbound_post,
    aws_api_gateway_integration_response.access_options_200,
    aws_api_gateway_integration_response.admin_members_options_200,
    aws_api_gateway_integration_response.admin_members_status_options_200,
    aws_api_gateway_integration_response.sms_inbound_options_200,
    aws_api_gateway_integration.admin_members_delete,
    aws_api_gateway_integration.admin_members_import_post,
    aws_api_gateway_integration_response.admin_members_import_options_200,
    aws_api_gateway_integration.admin_members_search_get,
    aws_api_gateway_integration_response.admin_members_search_options_200,
    aws_api_gateway_integration_response.admin_invite_preview_options_200,
    aws_api_gateway_integration_response.admin_invite_send_options_200,
    aws_api_gateway_integration_response.admin_invite_reminder_options_200,
    aws_api_gateway_integration.admin_invite_reminder_post
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.deploy.id
  stage_name    = "prod"
}

# -----------------------------
# Lambda permissions for API Gateway
# -----------------------------
resource "aws_lambda_permission" "allow_apigw_access" {
  statement_id  = "AllowApiGwInvokeAccess"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.access_request.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

resource "aws_lambda_permission" "allow_apigw_admin" {
  statement_id  = "AllowApiGwInvokeAdmin"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.admin_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

resource "aws_lambda_permission" "allow_apigw_sms" {
  statement_id  = "AllowApiGwInvokeSms"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sms_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}



resource "aws_lambda_permission" "allow_apigw_event" {
  statement_id  = "AllowApiGwInvokeEvent"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.event_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

resource "aws_lambda_permission" "allow_apigw_invite" {
  statement_id  = "AllowApiGwInvokeInvite"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.invite_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

# -----------------------------
# WAF (rate limit /access) + Association (valid for REST API stage)
# -----------------------------
resource "aws_wafv2_web_acl" "api_acl" {
  name  = "rsvp-api-acl"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "rsvp-api-acl"
    sampled_requests_enabled   = true
  }

  rule {
    name     = "rate-limit-access"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 500
        aggregate_key_type = "IP"

        scope_down_statement {
          byte_match_statement {
            search_string = "/access"
            field_to_match {
              uri_path {}
            }
            positional_constraint = "STARTS_WITH"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit-access"
      sampled_requests_enabled   = true
    }
  }
}

resource "aws_wafv2_web_acl_association" "api_stage" {
  resource_arn = "arn:aws:apigateway:${data.aws_region.current.name}::/restapis/${aws_api_gateway_rest_api.api.id}/stages/${aws_api_gateway_stage.prod.stage_name}"
  web_acl_arn  = aws_wafv2_web_acl.api_acl.arn
}

# -----------------------------
# CloudWatch Log Groups (retention)
# -----------------------------
resource "aws_cloudwatch_log_group" "access_request" {
  name              = "/aws/lambda/rsvp-access-request"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "admin_handler" {
  name              = "/aws/lambda/rsvp-admin-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "sms_handler" {
  name              = "/aws/lambda/rsvp-sms-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "event_handler" {
  name              = "/aws/lambda/rsvp-event-handler"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "invite_handler" {
  name              = "/aws/lambda/rsvp-invite-handler"
  retention_in_days = 30
}


output "api_base_url" {
  value = "https://api.rsvpsociety.com"
}

# -----------------------------
# CORS OPTIONS — /admin/invite/preview
# -----------------------------
resource "aws_api_gateway_method" "admin_invite_preview_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_preview.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_invite_preview_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_preview.id
  http_method = aws_api_gateway_method.admin_invite_preview_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "admin_invite_preview_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_preview.id
  http_method = aws_api_gateway_method.admin_invite_preview_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_invite_preview_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_preview.id
  http_method = aws_api_gateway_method.admin_invite_preview_options.http_method
  status_code = aws_api_gateway_method_response.admin_invite_preview_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'https://rsvpsociety.com'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
  }
}

# -----------------------------
# CORS OPTIONS — /admin/invite/send
# -----------------------------
resource "aws_api_gateway_method" "admin_invite_send_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_invite_send.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_invite_send_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_send.id
  http_method = aws_api_gateway_method.admin_invite_send_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "admin_invite_send_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_send.id
  http_method = aws_api_gateway_method.admin_invite_send_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_invite_send_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_invite_send.id
  http_method = aws_api_gateway_method.admin_invite_send_options.http_method
  status_code = aws_api_gateway_method_response.admin_invite_send_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'https://rsvpsociety.com'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'content-type,x-admin-token'"
  }
}

# -----------------------------
# CORS OPTIONS — missing endpoints (batch fix)
# -----------------------------

resource "aws_api_gateway_method" "admin_event_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_event_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event.id
  http_method = aws_api_gateway_method.admin_event_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}
resource "aws_api_gateway_method_response" "admin_event_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event.id
  http_method = aws_api_gateway_method.admin_event_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_event_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event.id
  http_method = aws_api_gateway_method.admin_event_options.http_method
  status_code = aws_api_gateway_method_response.admin_event_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "admin_members_gender_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_gender.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_gender_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_gender.id
  http_method = aws_api_gateway_method.admin_members_gender_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}
resource "aws_api_gateway_method_response" "admin_members_gender_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_gender.id
  http_method = aws_api_gateway_method.admin_members_gender_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_gender_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_gender.id
  http_method = aws_api_gateway_method.admin_members_gender_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_gender_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "admin_members_tier_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_tier.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_tier_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_tier.id
  http_method = aws_api_gateway_method.admin_members_tier_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}
resource "aws_api_gateway_method_response" "admin_members_tier_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_tier.id
  http_method = aws_api_gateway_method.admin_members_tier_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_tier_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_tier.id
  http_method = aws_api_gateway_method.admin_members_tier_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_tier_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "admin_members_attendance_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_attendance.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_attendance_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_attendance.id
  http_method = aws_api_gateway_method.admin_members_attendance_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}
resource "aws_api_gateway_method_response" "admin_members_attendance_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_attendance.id
  http_method = aws_api_gateway_method.admin_members_attendance_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_attendance_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_attendance.id
  http_method = aws_api_gateway_method.admin_members_attendance_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_attendance_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "event_current_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event_current.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_current_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_current.id
  http_method = aws_api_gateway_method.event_current_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}
resource "aws_api_gateway_method_response" "event_current_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_current.id
  http_method = aws_api_gateway_method.event_current_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "event_current_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_current.id
  http_method = aws_api_gateway_method.event_current_options.http_method
  status_code = aws_api_gateway_method_response.event_current_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}

resource "aws_api_gateway_method" "event_public_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.event_public.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "event_public_options" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_public.id
  http_method = aws_api_gateway_method.event_public_options.http_method
  type        = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}
resource "aws_api_gateway_method_response" "event_public_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_public.id
  http_method = aws_api_gateway_method.event_public_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "event_public_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.event_public.id
  http_method = aws_api_gateway_method.event_public_options.http_method
  status_code = aws_api_gateway_method_response.event_public_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}
