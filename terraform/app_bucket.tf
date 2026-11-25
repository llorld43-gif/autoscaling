# Bucket privado con el código de la app (index.html, server.py).
# Las instancias lo descargan al arrancar: así el user_data no supera el límite de 16 KB de AWS.
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "app" {
  bucket        = "${var.project_name}-app-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "app" {
  bucket                  = aws_s3_bucket.app.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "server_py" {
  bucket = aws_s3_bucket.app.id
  key    = "${local.app_version}/server.py"
  source = "${path.module}/../app/server.py"
  etag   = filemd5("${path.module}/../app/server.py")
}

resource "aws_s3_object" "index_html" {
  bucket       = aws_s3_bucket.app.id
  key          = "${local.app_version}/index.html"
  source       = "${path.module}/../app/index.html"
  content_type = "text/html"
  etag         = filemd5("${path.module}/../app/index.html")
}
