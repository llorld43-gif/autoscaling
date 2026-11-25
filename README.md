# AWS Auto Scaling Group + Load Balancer + Nginx Demo

Proyecto de infraestructura como código (Terraform) desplegado automáticamente mediante **GitHub Actions con OIDC** reutilizando el bootstrap compartido del repositorio `agentes` (sin ejecutar un bootstrap nuevo).

## Arquitectura

- **Instancias**: EC2 `t3.micro` (capa gratuita) con Amazon Linux 2023 + Nginx + API Python ligera.
- **Auto Scaling Group**:
  - Límite inferior (`min_size`): **1 instancia**
  - Límite superior (`max_size`): **4 instancias**
- **Application Load Balancer (ALB)**: Balancea el tráfico HTTP entre las instancias saludables del ASG.
- **Alarmas CloudWatch**:
  - **Scale Out (`autoscaling-demo-high-cpu`)**: `CPUUtilization >= 50%` durante 2 períodos de 1 minuto → `+1` instancia.
  - **Scale In (`autoscaling-demo-low-cpu`)**: `CPUUtilization <= 20%` durante 2 períodos de 1 minuto → `-1` instancia.

## Estructura del Proyecto

```text
autoscaling/
├── .github/
│   ├── scripts/smoke_test.py     # Smoke test post-despliegue
│   └── workflows/deploy.yml      # Pipeline CI/CD con OIDC
├── app/
│   ├── index.html                # Dashboard visual (Estrés CPU + Balanceo)
│   └── server.py                 # API local (/api/whoami, /api/stress/start|stop)
├── scripts/
│   └── validate_scaling_e2e.py   # Validación E2E de alarmas y balanceo
└── terraform/
    ├── alb.tf
    ├── asg.tf
    ├── outputs.tf
    ├── provider.tf               # Backend S3/DynamoDB compartido de agentes
    ├── scaling_alarms.tf
    ├── user_data.sh
    ├── variables.tf
    └── vpc.tf
```

## Plan de ejecución

Ver [PLAN.md](PLAN.md): requisitos, fases, causa y corrección del último fallo del pipeline y notas de costos de capa gratuita.

## Validación E2E de Alarmas

Una vez desplegado por GitHub Actions, puedes validar automáticamente el escalamiento hacia arriba, el balanceo y el escalamiento hacia abajo con:

```bash
python scripts/validate_scaling_e2e.py --alb-url http://<ALB_DNS>
```

O desde GitHub: **Actions → Pipeline de Despliegue → Run workflow** con `run_scaling_test = true`.
