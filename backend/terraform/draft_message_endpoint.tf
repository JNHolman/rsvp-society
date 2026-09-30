# =============================================================================
# draft_message_endpoint.tf
#
# POST /admin/event/draft-message
#
# Drafts invite + day-before + day-of templates in Jade's voice for the operator
# to review, edit, and lock. Does NOT send anything — returns drafts only.
# Reuses the cached Jade persona prompt. The invite is guaranteed venue/address-free.
# =============================================================================

# ── Resource: /admin/event/draft-message ──────────────────────────────────────
resource "aws_api_gateway_resource" "admin_event_draft_message" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_event.id
  path_part   = "draft-message"
}

# ── POST ──────────────────────────────────────────────────────────────────────
resource "aws_api_gateway_method" "admin_event_draft_message_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event_draft_message.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_event_draft_message_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_event_draft_message.id
  http_method             = aws_api_gateway_method.admin_event_draft_message_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

# ── OPTIONS (CORS preflight) ──────────────────────────────────────────────────
resource "aws_api_gateway_method" "admin_event_draft_message_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_event_draft_message.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_event_draft_message_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_event_draft_message.id
  http_method       = aws_api_gateway_method.admin_event_draft_message_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}

resource "aws_api_gateway_method_response" "admin_event_draft_message_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event_draft_message.id
  http_method = aws_api_gateway_method.admin_event_draft_message_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "admin_event_draft_message_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_event_draft_message.id
  http_method = aws_api_gateway_method.admin_event_draft_message_options.http_method
  status_code = aws_api_gateway_method_response.admin_event_draft_message_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}
