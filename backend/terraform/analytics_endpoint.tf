# =============================================================================
# analytics_endpoint.tf
#
# GET /admin/event/analytics
#
# Returns post-event summary from rsvp-event-invites:
#   - invited / confirmed / declined / no_response / attended totals
#   - confirm rate, decline rate, show rate, ghost rate
#   - breakdown by gender (M/F/O)
#   - breakdown by tier (1/2)
#
# All data comes from a primary key query on eventId = "current".
# No scan. attendedAt is written back to the invite record by
# member_store.record_attendance() at check-in time.
# =============================================================================

# ── Resource: /admin/event/analytics ──────────────────────────────────────────
resource "aws_api_gateway_resource" "admin_event_analytics" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_event.id
  path_part   = "analytics"
}

# ── GET ───────────────────────────────────────────────────────────────────────
resource "aws_api_gateway_method" "admin_event_analytics_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event_analytics.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_event_analytics_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event_analytics.id
  http_method             = aws_api_gateway_method.admin_event_analytics_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# ── OPTIONS (CORS preflight) ──────────────────────────────────────────────────
resource "aws_api_gateway_method" "admin_event_analytics_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event_analytics.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_event_analytics_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_event_analytics.id
  http_method       = aws_api_gateway_method.admin_event_analytics_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}

resource "aws_api_gateway_method_response" "admin_event_analytics_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event_analytics.id
  http_method = aws_api_gateway_method.admin_event_analytics_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_event_analytics_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event_analytics.id
  http_method = aws_api_gateway_method.admin_event_analytics_options.http_method
  status_code = aws_api_gateway_method_response.admin_event_analytics_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_origins
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}
