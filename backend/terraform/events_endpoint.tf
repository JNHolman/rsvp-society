# =============================================================================
# events_endpoint.tf
#
# /admin/events event-manager API routes
# =============================================================================

resource "aws_api_gateway_resource" "admin_events" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "events"
}

# GET /admin/events
resource "aws_api_gateway_method" "admin_events_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events.id
  http_method             = aws_api_gateway_method.admin_events_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# POST /admin/events
resource "aws_api_gateway_method" "admin_events_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events.id
  http_method             = aws_api_gateway_method.admin_events_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# DELETE /admin/events — guarded archive/hard-delete handling lives in the admin Lambda
resource "aws_api_gateway_method" "admin_events_delete" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events.id
  http_method   = "DELETE"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_delete" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events.id
  http_method             = aws_api_gateway_method.admin_events_delete.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# POST /admin/events/set-active
resource "aws_api_gateway_resource" "admin_events_set_active" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_events.id
  path_part   = "set-active"
}

resource "aws_api_gateway_method" "admin_events_set_active_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events_set_active.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_set_active_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events_set_active.id
  http_method             = aws_api_gateway_method.admin_events_set_active_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# POST /admin/events/archive
resource "aws_api_gateway_resource" "admin_events_archive" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_events.id
  path_part   = "archive"
}

resource "aws_api_gateway_method" "admin_events_archive_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events_archive.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_archive_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events_archive.id
  http_method             = aws_api_gateway_method.admin_events_archive_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# POST /admin/events/duplicate
resource "aws_api_gateway_resource" "admin_events_duplicate" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_events.id
  path_part   = "duplicate"
}

resource "aws_api_gateway_method" "admin_events_duplicate_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events_duplicate.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_duplicate_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events_duplicate.id
  http_method             = aws_api_gateway_method.admin_events_duplicate_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}


# POST /admin/events/finalize-attendance
resource "aws_api_gateway_resource" "admin_events_finalize_attendance" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_events.id
  path_part   = "finalize-attendance"
}

resource "aws_api_gateway_method" "admin_events_finalize_attendance_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_events_finalize_attendance.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_finalize_attendance_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_events_finalize_attendance.id
  http_method             = aws_api_gateway_method.admin_events_finalize_attendance_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# CORS helpers for /admin/events and children
locals {
  admin_events_cors_resources = {
    base       = aws_api_gateway_resource.admin_events.id
    set_active = aws_api_gateway_resource.admin_events_set_active.id
    archive    = aws_api_gateway_resource.admin_events_archive.id
    duplicate  = aws_api_gateway_resource.admin_events_duplicate.id
    finalize_attendance = aws_api_gateway_resource.admin_events_finalize_attendance.id
  }
}

resource "aws_api_gateway_method" "admin_events_options" {
  for_each      = local.admin_events_cors_resources
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = each.value
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_events_options" {
  for_each          = local.admin_events_cors_resources
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = each.value
  http_method       = aws_api_gateway_method.admin_events_options[each.key].http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}

resource "aws_api_gateway_method_response" "admin_events_options_200" {
  for_each    = local.admin_events_cors_resources
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.admin_events_options[each.key].http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_events_options_200" {
  for_each    = local.admin_events_cors_resources
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.admin_events_options[each.key].http_method
  status_code = aws_api_gateway_method_response.admin_events_options_200[each.key].status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}
