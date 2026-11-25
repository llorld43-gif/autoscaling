locals {
  # Hash del código de la app: cambia con cada modificación de app/ y se expone
  # en /api/health para que el smoke test verifique que corre la versión nueva.
  app_version = substr(sha1(join("", [
    filemd5("${path.module}/../app/server.py"),
    filemd5("${path.module}/../app/index.html"),
    filemd5("${path.module}/user_data.sh"),
  ])), 0, 12)
}

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_launch_template" "app" {
  name_prefix   = "${var.project_name}-lt-"
  image_id      = data.aws_ssm_parameter.al2023_ami.value
  instance_type = var.instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.ec2_monitor.name
  }

  # Monitoreo detallado (1 minuto) para que las alarmas de Auto Scaling respondan rápido
  monitoring {
    enabled = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.ec2.id]
  }

  user_data = base64encode(templatefile("${path.module}/user_data.sh", {
    app_bucket       = aws_s3_bucket.app.id
    aws_region       = var.aws_region
    asg_name         = "${var.project_name}-asg"
    high_alarm_name  = "${var.project_name}-high-cpu"
    low_alarm_name   = "${var.project_name}-low-cpu"
    target_group_arn = aws_lb_target_group.app.arn
    app_version      = local.app_version
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.project_name}-nginx-node"
    }
  }

  # Los archivos deben existir en S3 antes de que arranque una instancia
  depends_on = [aws_s3_object.server_py, aws_s3_object.index_html]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "app" {
  name                      = "${var.project_name}-asg"
  min_size                  = var.min_size
  max_size                  = var.max_size
  desired_capacity          = var.desired_capacity
  vpc_zone_identifier       = [aws_subnet.public_a.id, aws_subnet.public_b.id]
  target_group_arns         = [aws_lb_target_group.app.arn]
  health_check_type         = "ELB"
  health_check_grace_period = 120
  default_cooldown          = 60

  # Versión explícita (no "$Latest"): así cada cambio del launch template
  # modifica este bloque y Terraform dispara el instance refresh.
  launch_template {
    id      = aws_launch_template.app.id
    version = aws_launch_template.app.latest_version
  }

  # Lanza la instancia nueva antes de terminar la vieja (sin caída con 1 instancia)
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 100
      max_healthy_percentage = 200
      instance_warmup        = 90
    }
  }

  # Métricas de grupo del ASG (gratuitas) para graficar instancias en el dashboard
  enabled_metrics = [
    "GroupDesiredCapacity",
    "GroupInServiceInstances",
  ]
  metrics_granularity = "1Minute"

  tag {
    key                 = "Name"
    value               = "${var.project_name}-asg-instance"
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}
