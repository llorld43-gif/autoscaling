variable "aws_region" {
  description = "Región de AWS"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Nombre del proyecto"
  type        = string
  default     = "autoscaling-demo"
}

variable "environment" {
  description = "Ambiente de despliegue"
  type        = string
  default     = "prod"
}

variable "instance_type" {
  description = "Tipo de instancia EC2 (t3.micro elegible para capa gratuita)"
  type        = string
  default     = "t3.micro"
}

variable "min_size" {
  description = "Límite inferior de instancias en el Auto Scaling Group"
  type        = number
  default     = 1
}

variable "max_size" {
  description = "Límite superior de instancias en el Auto Scaling Group"
  type        = number
  default     = 4
}

variable "desired_capacity" {
  description = "Capacidad deseada inicial del Auto Scaling Group"
  type        = number
  default     = 1
}

variable "scale_out_cpu_threshold" {
  description = "Umbral de CPU (%) para escalar hacia arriba"
  type        = number
  default     = 50
}

variable "scale_in_cpu_threshold" {
  description = "Umbral de CPU (%) para escalar hacia abajo"
  type        = number
  default     = 20
}
