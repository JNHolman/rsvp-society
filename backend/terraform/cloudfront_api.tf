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
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0

    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Content-Type", "x-admin-token", "Origin"]

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
    acm_certificate_arn      = aws_acm_certificate.api.arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  depends_on = [aws_acm_certificate.api]

  tags = {
    Project     = "rsvp-society"
    Environment = "prod"
  }
}

output "cloudfront_api_domain" {
  value       = aws_cloudfront_distribution.api.domain_name
  description = "Add this as CNAME for api.rsvpsociety.com in Squarespace"
}
