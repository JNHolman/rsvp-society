# CloudFront Function — dynamic CORS origin header
# Allows both https://rsvpsociety.com and https://www.rsvpsociety.com
# without changing 14 API Gateway MOCK integrations
resource "aws_cloudfront_function" "cors_origin" {
  name    = "rsvp-cors-allow-origin"
  runtime = "cloudfront-js-2.0"
  comment = "Set Access-Control-Allow-Origin dynamically based on request Origin header"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var response = event.response;
      var request  = event.request;
      var headers  = response.headers;
      var origin   = request.headers['origin'] ? request.headers['origin'].value : '';
      var allowed  = ['https://rsvpsociety.com', 'https://www.rsvpsociety.com'];
      if (allowed.indexOf(origin) !== -1) {
        headers['access-control-allow-origin'] = { value: origin };
        headers['vary'] = { value: 'Origin' };
      }
      return response;
    }
  EOT
}

# ─────────────────────────────────────────────────────────────────────────────
# ACM Certificate for api.rsvpsociety.com
# Must be in us-east-1 for CloudFront
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_acm_certificate" "api" {
  domain_name       = "api.rsvpsociety.com"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Project     = "rsvp-society"
    Environment = "prod"
  }
}

# Terraform will block here until the cert is issued.
# Before running terraform apply, add the CNAME from
# acm_dns_validation_record output to Squarespace DNS.
resource "aws_acm_certificate_validation" "api" {
  certificate_arn = aws_acm_certificate.api.arn
}

output "acm_dns_validation_record" {
  value       = aws_acm_certificate.api.domain_validation_options
  description = "Add this CNAME record to Squarespace DNS to validate the certificate"
}

# ─────────────────────────────────────────────────────────────────────────────
# CloudFront distribution
# ─────────────────────────────────────────────────────────────────────────────
locals {
  api_gateway_origin_id = "rsvp-api-gateway"
  api_gateway_domain    = "${aws_api_gateway_rest_api.api.id}.execute-api.${data.aws_region.current.name}.amazonaws.com"
}

resource "aws_cloudfront_distribution" "api" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "RSVP Society API Gateway distribution"
  price_class     = "PriceClass_100"
  aliases         = ["api.rsvpsociety.com"]

  origin {
    domain_name = local.api_gateway_domain
    origin_id   = local.api_gateway_origin_id
    origin_path = "/prod"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD", "OPTIONS"]
    target_origin_id       = local.api_gateway_origin_id
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    function_association {
      event_type   = "viewer-response"
      function_arn = aws_cloudfront_function.cors_origin.arn
    }
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0

    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Content-Type", "x-admin-token", "Origin", "Access-Control-Request-Headers", "Access-Control-Request-Method", "openphone-signature"]

      cookies {
        forward = "none"
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.api.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  depends_on = [aws_acm_certificate_validation.api]

  tags = {
    Project     = "rsvp-society"
    Environment = "prod"
  }
}

output "cloudfront_api_domain" {
  value       = aws_cloudfront_distribution.api.domain_name
  description = "Add this as CNAME for api.rsvpsociety.com in Squarespace"
}
