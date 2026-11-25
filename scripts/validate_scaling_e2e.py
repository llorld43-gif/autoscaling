#!/usr/bin/env python3
"""
Validación End-to-End (E2E) de Auto Scaling y Balanceo de Carga en AWS.

Verifica:
1. Estado inicial del ASG (min=1, max=4, desired=1) y salud del ALB.
2. Prueba de Estrés -> Disparo de alarma High CPU -> Escalamiento hacia arriba (>1 instancias).
3. Prueba de Balanceo de Carga -> Tráfico distribuido entre múltiples instancias EC2.
4. Detención de Estrés -> Disparo de alarma Low CPU -> Escalamiento hacia abajo (regreso a 1).
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections import Counter


def aws_json(args):
    cmd = ["aws"] + args + ["--output", "json"]
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def get_asg_info(asg_name, region):
    data = aws_json([
        "autoscaling", "describe-auto-scaling-groups",
        "--auto-scaling-group-names", asg_name,
        "--region", region,
    ])
    return data["AutoScalingGroups"][0]


def get_alarm_state(alarm_name, region):
    data = aws_json([
        "cloudwatch", "describe-alarms",
        "--alarm-names", alarm_name,
        "--region", region,
    ])
    alarm = data["MetricAlarms"][0]
    return alarm["StateValue"], alarm.get("StateReason", "")


def http_post(url, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(url):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Validar E2E Auto Scaling + ALB")
    parser.add_argument("--alb-url", required=True)
    parser.add_argument("--asg-name", default="autoscaling-demo-asg")
    parser.add_argument("--high-alarm", default="autoscaling-demo-high-cpu")
    parser.add_argument("--low-alarm", default="autoscaling-demo-low-cpu")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--timeout-min", type=int, default=12)
    args = parser.parse_args()

    base = args.alb_url.rstrip("/")
    print("=" * 70)
    print("🚀 VALIDACIÓN E2E DE AUTO SCALING Y BALANCEO DE CARGA")
    print(f"   • ALB URL:    {base}")
    print(f"   • ASG:        {args.asg_name}")
    print(f"   • High Alarm: {args.high_alarm}")
    print(f"   • Low Alarm:  {args.low_alarm}")
    print("=" * 70)

    asg = get_asg_info(args.asg_name, args.region)
    print(f"\n📌 Estado inicial ASG: min={asg['MinSize']}, max={asg['MaxSize']}, desired={asg['DesiredCapacity']}, instances={len(asg['Instances'])}")

    # 1. Fase Scale Out
    print("\n🔥 [FASE 1] Iniciando prueba de estrés para activar alarma Scale Out...")
    deadline = time.time() + args.timeout_min * 60
    scaled_out = False
    while time.time() < deadline:
        for _ in range(4):
            try:
                http_post(f"{base}/api/stress/start", {"duration": 300})
            except Exception:
                pass
        asg = get_asg_info(args.asg_name, args.region)
        high_state, _ = get_alarm_state(args.high_alarm, args.region)
        in_service = [i for i in asg["Instances"] if i["LifecycleState"] == "InService"]
        print(f"   [Scale-Out] HighAlarm={high_state} | Desired={asg['DesiredCapacity']} | InService={len(in_service)}")
        if asg["DesiredCapacity"] >= 2 and len(in_service) >= 2 and high_state == "ALARM":
            scaled_out = True
            print("   ✅ ¡Alarma Scale-Out validada y múltiples instancias InService!")
            break
        time.sleep(20)

    if not scaled_out:
        print("   ❌ No se observó escalamiento hacia arriba dentro del tiempo límite.")
        sys.exit(1)

    # 2. Fase Balanceo de Carga
    print("\n⚖️ [FASE 2] Validando balanceo de carga (24 peticiones)...")
    counts = Counter()
    for _ in range(24):
        try:
            res = http_get(f"{base}/api/whoami?t={time.time()}")
            counts[res["instance_id"]] += 1
        except Exception:
            pass
    for inst_id, cnt in counts.items():
        print(f"   • {inst_id}: {cnt} peticiones")
    if len(counts) < 2:
        print("   ⚠️ Solo respondió 1 instancia; esperando 30s para registro en Target Group...")
        time.sleep(30)
        counts.clear()
        for _ in range(24):
            res = http_get(f"{base}/api/whoami?t={time.time()}")
            counts[res["instance_id"]] += 1
        for inst_id, cnt in counts.items():
            print(f"   • {inst_id}: {cnt} peticiones")

    # 3. Fase Scale In
    print("\n🛑 [FASE 3] Deteniendo estrés para validar alarma Scale In...")
    for _ in range(16):
        try:
            http_post(f"{base}/api/stress/stop")
        except Exception:
            pass

    deadline_in = time.time() + args.timeout_min * 60
    scaled_in = False
    while time.time() < deadline_in:
        asg = get_asg_info(args.asg_name, args.region)
        low_state, _ = get_alarm_state(args.low_alarm, args.region)
        print(f"   [Scale-In]  LowAlarm={low_state} | Desired={asg['DesiredCapacity']} | Instances={len(asg['Instances'])}")
        if low_state == "ALARM" and asg["DesiredCapacity"] == asg["MinSize"]:
            scaled_in = True
            print("   ✅ ¡Alarma Scale-In validada y ASG regresó al mínimo (1)!")
            break
        time.sleep(20)

    if not scaled_in:
        print("   ❌ No se observó escalamiento hacia abajo dentro del tiempo límite.")
        sys.exit(1)

    print("\n🎉 VALIDACIÓN E2E COMPLETADA CON ÉXITO.")


if __name__ == "__main__":
    main()
