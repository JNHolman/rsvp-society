# /admin/members/confirmed — returns confirmed invitees for current event
resource "aws_api_gateway_resource" "admin_members_confirmed" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.admin_members.id
  path_part   = "confirmed"
}

resource "aws_api_gateway_method" "admin_members_confirmed_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_confirmed.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "admin_members_confirmed_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.admin_members_confirmed.id
  http_method             = aws_api_gateway_method.admin_members_confirmed_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.admin_handler.invoke_arn
}

resource "aws_api_gateway_method" "admin_members_confirmed_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.admin_members_confirmed.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "admin_members_confirmed_options" {
  rest_api_id       = aws_api_gateway_rest_api.api.id
  resource_id       = aws_api_gateway_resource.admin_members_confirmed.id
  http_method       = aws_api_gateway_method.admin_members_confirmed_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = local.cors_mock_request_template }
}
resource "aws_api_gateway_method_response" "admin_members_confirmed_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_confirmed.id
  http_method = aws_api_gateway_method.admin_members_confirmed_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}
resource "aws_api_gateway_integration_response" "admin_members_confirmed_options_200" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.admin_members_confirmed.id
  http_method = aws_api_gateway_method.admin_members_confirmed_options.http_method
  status_code = aws_api_gateway_method_response.admin_members_confirmed_options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin_expr
    "method.response.header.Access-Control-Allow-Methods" = local.cors_methods
    "method.response.header.Access-Control-Allow-Headers" = local.cors_headers
  }
}
