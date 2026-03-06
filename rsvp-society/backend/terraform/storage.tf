# -----------------------------
# storage.tf
# S3 + CloudFront for /pics
# Deploy when you have photos ready.
# Usage: drop photos into s3://rsvp-society-pics-prod/YYYY-MM-event-name/
# -----------------------------

# -----------------------------
# ACM Certificate for pics.rsvpsociety.com
# Must be in us-east-1 for CloudFront
# -----------------------------
resource "aws_acm_certificate" "pics" {
  domain_name       = "pics.rsvpsociety.com"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Terraform will block here until the cert is issued.
# Before running terraform apply, add the CNAME from
# pics_acm_dns_validation_record output to Squarespace DNS.
resource "aws_acm_certificate_validation" "pics" {
  certificate_arn = aws_acm_certificate.pics.arn
}

output "pics_acm_dns_validation_record" {
  value       = aws_acm_certificate.pics.domain_validation_options
  description = "Add this CNAME record to Squarespace DNS to validate the pics certificate"
}

# -----------------------------
# S3 Bucket (private — CloudFront only)
# -----------------------------
resource "aws_s3_bucket" "pics" {
  bucket = "rsvp-society-pics-prod"
}

resource "aws_s3_bucket_public_access_block" "pics" {
  bucket                  = aws_s3_bucket.pics.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "pics" {
  bucket = aws_s3_bucket.pics.id
  versioning_configuration {
    status = "Enabled"
  }
}

# -----------------------------
# CloudFront Origin Access Control (OAC)
# Modern replacement for OAI — do not use OAI
# -----------------------------
resource "aws_cloudfront_origin_access_control" "pics" {
  name                              = "rsvp-pics-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# -----------------------------
# CloudFront Distribution
# -----------------------------
resource "aws_cloudfront_distribution" "pics" {
  enabled             = true
  comment             = "RSVP Society — event photos"
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # US + Europe only — cheapest
  aliases             = ["pics.rsvpsociety.com"]

  depends_on = [aws_acm_certificate_validation.pics]

  origin {
    domain_name              = aws_s3_bucket.pics.bucket_regional_domain_name
    origin_id                = "rsvp-pics-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.pics.id
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "rsvp-pics-s3"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    # Photos don't change — cache for 7 days at the edge
    min_ttl     = 0
    default_ttl = 604800  # 7 days
    max_ttl     = 2592000 # 30 days
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.pics.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# -----------------------------
# S3 Bucket Policy — allow CloudFront OAC only
# -----------------------------
resource "aws_s3_bucket_policy" "pics" {
  bucket = aws_s3_bucket.pics.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudFrontOAC"
        Effect = "Allow"
        Principal = {
          Service = "cloudfront.amazonaws.com"
        }
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.pics.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.pics.arn
          }
        }
      }
    ]
  })
}

# -----------------------------
# Outputs
# -----------------------------
output "pics_cloudfront_url" {
  value       = "https://pics.rsvpsociety.com"
  description = "Base URL for event photos. Append /YYYY-MM-event-name/filename.jpg"
}

output "pics_s3_bucket" {
  value       = aws_s3_bucket.pics.bucket
  description = "Upload photos here: aws s3 cp ./photos s3://rsvp-society-pics-prod/YYYY-MM-event-name/ --recursive"
}

