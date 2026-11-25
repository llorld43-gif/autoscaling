output "alb_dns_name" {
  description = "DNS público del Application Load Balancer"
  value       = aws_lb.app.dns_name
}

output "alb_url" {
  description = "URL HTTP pública de la aplicación Nginx"
  value       = "http://${aws_lb.app.dns_name}"
}

output "asg_name" {
  description = "Nombre del Auto Scaling Group"
  value       = aws_autoscaling_group.app.name
}

output "high_cpu_alarm_name" {
  description = "Nombre de la alarma CloudWatch para escalar hacia arriba (Scale Out)"
  value       = aws_cloudwatch_metric_alarm.high_cpu.alarm_name
}

output "low_cpu_alarm_name" {
  description = "Nombre de la alarma CloudWatch para escalar hacia abajo (Scale In)"
  value       = aws_cloudwatch_metric_alarm.low_cpu.alarm_name
}

output "target_group_arn" {
  description = "ARN del Target Group del ALB"
  value       = aws_lb_target_group.app.arn
}

output "app_version" {
  description = "Hash de la versión de la app desplegada (lo devuelve /api/health)"
  value       = local.app_version
}
