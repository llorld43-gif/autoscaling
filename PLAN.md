# Plan de ejecución — AWS Auto Scaling Lab

Objetivo: desplegar por GitHub Actions un Auto Scaling Group de `t3.micro` (mín 1, máx 4) detrás de un ALB, con una página Nginx que permita lanzar una prueba de estrés y una prueba de balanceo, y validar que las alarmas de CloudWatch escalan hacia arriba y hacia abajo.

**Criterio de éxito:** el pipeline `deploy.yml` termina en verde en un push a `main` (infraestructura + rollout + smoke test con la versión nueva). La validación 1 → 4 → 1 se demuestra desde la página o con el job E2E manual.

---

## Requisitos y dónde se cumplen

| Requisito | Estado | Dónde |
|---|---|---|
| ASG con mínimo 1 y máximo 4 | ✅ | `terraform/variables.tf` (`min_size`, `max_size`), `terraform/asg.tf` |
| Instancias `t3.micro` (capa gratuita) | ✅ | `variables.tf` → `instance_type` |
| Alarma de scale-out (CPU ≥ 50%, 2×1 min → +1) | ✅ | `terraform/scaling_alarms.tf` |
| Alarma de scale-in (CPU ≤ 20%, 2×1 min → −1) | ✅ | `terraform/scaling_alarms.tf` |
| Página Nginx con botón de prueba de estrés | ✅ | `app/index.html` + `app/server.py` (`/api/stress/cluster/*`) |
| Prueba de balanceo de carga visual | ✅ | `app/index.html` (topología animada + reparto por instancia) |
| Validar que las alarmas son efectivas | ✅ | Checklist en la página + `scripts/validate_scaling_e2e.py` (atribuye cada actividad del ASG a su alarma) |
| Bootstrap de `agentes` sirve para todos los repos | ✅ | `agentes/bootstrap/main.tf`: trust `repo:llorld43-gif/*:*`, ya aplicado en AWS |
| Sin bootstrap nuevo en este proyecto | ✅ | `terraform/provider.tf` reutiliza el bucket/tabla de `agentes`; secreto `AWS_ROLE_ARN` apunta al rol compartido |
| Despliegue por GitHub Actions | ⚠️ → ✅ | Último run falló; corregido en esta revisión (ver Fase 1) |

---

## Fase 0 — Prerrequisitos (ya hechos)

- [x] Bootstrap de `agentes` aplicado: proveedor OIDC, rol `citas-escolares-github-oidc-role`, bucket de estado y tabla de locks. `terraform plan` en `agentes/bootstrap` → *No changes*.
- [x] Trust policy del rol acepta cualquier repo de `llorld43-gif` (formato clásico e inmutable del claim `sub`).
- [x] Política del pipeline incluye EC2, Auto Scaling, ELB, CloudWatch, SSM y roles/instance profiles `autoscaling-*`.
- [x] Secreto `AWS_ROLE_ARN` creado en `llorld43-gif/autoscaling`.

## Fase 1 — Corregir el pipeline (esta revisión)

**Causa del fallo del último run:** el ASG usaba `version = "$Latest"`. Al cambiar el launch template, el bloque `launch_template` del ASG no cambiaba y Terraform **nunca lanzó el instance refresh**. La única instancia seguía con la versión 1 del template (sin `/api/cluster`) y el smoke test recibía 404.

Cambios:
- [x] `asg.tf`: `version = aws_launch_template.app.latest_version` → cada cambio de `app/` o `user_data.sh` dispara un instance refresh.
- [x] `asg.tf`: refresh *launch-before-terminate* (`min_healthy 100%`, `max_healthy 200%`) para no quedarse sin instancias con capacidad 1.
- [x] `app_version` (hash de `app/` + `user_data.sh`) inyectado en la instancia y expuesto en `/api/health`.
- [x] `deploy.yml`: `fmt -check` + `validate`, espera a que el instance refresh termine y el smoke test exige que el ALB sirva solo la versión nueva.
- [x] El código de la app (`index.html`, `server.py`) se sube a un bucket S3 privado (`terraform/app_bucket.tf`) y la instancia lo descarga al arrancar. Antes iba embebido en el user_data y, con la página nueva, superaba el límite de 16 KB de AWS (`InvalidUserData.Malformed`).
- [x] Métricas de grupo del ASG (`GroupInServiceInstances`, gratuitas) para graficar el número de instancias.

## Fase 2 — Página Nginx mejorada (esta revisión)

- [x] Topología en vivo: ALB → 4 slots (ocupados / libres) con CPU, AZ, salud en el target group y estado de estrés.
- [x] Peticiones animadas del ALB a la instancia que respondió + contador por instancia y métrica de equilibrio.
- [x] Gráfico de 15 min con CPU promedio, umbrales 50/20% y número de instancias (eje derecho).
- [x] Checklist de validación que se marca sola con datos reales: estrés → alarma ALARM → 4/4 → balanceo ≥ 2 nodos → stop → alarma scale-in → 1/1.
- [x] Actividades del ASG etiquetadas con la alarma que las causó.
- [x] Salvaguarda: el navegador deja de reenviar estrés a los 25 min; cada orden de estrés expira sola a los 5 min.
- [x] Corregido: texto basura `HTMLEOF` al final del HTML.

## Fase 3 — Desplegar y verificar

1. `git push` a `main` → el pipeline aplica Terraform, espera el rollout y ejecuta el smoke test.
2. Abrir la URL del ALB (aparece en el resumen del run).
3. **Prueba visual:** *Iniciar estrés* → esperar 4/4 (≈ 6–10 min) → *Enviar 40 peticiones* → *Detener estrés* → esperar 1/1 (≈ 6–10 min). La checklist debe quedar completa.
4. **Prueba automatizada (opcional):** *Actions → Run workflow → `run_scaling_test = true`*. El job 3 hace 1 → 4 → 1, comprueba balanceo entre ≥ 2 instancias y que cada cambio de capacidad lo causó la alarma correspondiente.

## Fase 4 — Limpieza

Para no gastar horas de capa gratuita: `terraform destroy` en `terraform/` (con credenciales locales) o dejar el ASG en 1 instancia.

---

## Costos y capa gratuita (a tener en cuenta)

- **EC2 t3.micro:** 750 h/mes gratis. Con 4 instancias durante la prueba se consumen 4 h por hora de prueba.
- **ALB:** 750 h/mes + 15 LCU gratis en cuentas dentro de los 12 meses de capa gratuita.
- **Monitoreo detallado (1 min):** *no es gratis* (~0,02 USD/instancia·día). Se mantiene porque con monitoreo básico (5 min) cada paso del escalado tardaría ~10 min.
- **T3 en modo *unlimited* (por defecto):** el estrés al 100% consume créditos de CPU por encima del baseline (10%); una prueba de ~25 min con hasta 4 instancias cuesta céntimos. El modo *standard* no sirve: sin créditos iniciales la CPU queda limitada al 10% y nunca dispara la alarma.
- **IPv4 públicas:** 750 h/mes gratis en EC2; con 4 instancias + ALB se puede superar si se deja escalado mucho tiempo.

## Riesgos pendientes / mejoras futuras

- La política IAM del bootstrap solo permite crear roles con prefijo `citas-escolares-*` o `autoscaling-*`. Un repo nuevo que cree roles IAM necesitará usar uno de esos prefijos o ampliar `pipeline_policy` en `agentes/bootstrap` y reaplicarlo localmente.
- Las APIs de estrés no tienen autenticación: cualquiera con la URL del ALB puede lanzar estrés. Aceptable para un laboratorio; no dejar el entorno levantado indefinidamente.
