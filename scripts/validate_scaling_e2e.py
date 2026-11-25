#!/usr/bin/env python3
"""
Validación End-to-End (E2E) de Auto Scaling (1 -> 4 -> 1) y Balanceo de Carga en AWS.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections import Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


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
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(url):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Validar E2E Auto Scaling (1->4->1) + ALB")
    parser.add_argument("--alb-url", required=True)
    parser.add_argument("--asg-name", default="autoscaling-demo-asg")
    parser.add_argument("--high-alarm", default="autoscaling-demo-high-cpu")
    parser.add_argument("--low-alarm", default="autoscaling-demo-low-cpu")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--timeout-min", type=int, default=18)
    args = parser.parse_args()

    base = args.alb_url.rstrip("/")
    print("=" * 70)
    print("🚀 VALIDACIÓN E2E DE AUTO SCALING (1 -> 4 -> 1) Y BALANCEO")
    print(f"   • ALB URL:    {base}")
    print(f"   • ASG:        {args.asg_name}")
    print("=" * 70)

    asg = get_asg_info(args.asg_name, args.region)
    print(f"\n📌 Estado inicial ASG: min={asg['MinSize']}, max={asg['MaxSize']}, desired={asg['DesiredCapacity']}")

    # 1. Fase Scale Out hasta max_size (4)
    print("\n🔥 [FASE 1] Iniciando estrés en todo el clúster para escalar hasta max_size=4...")
    deadline = time.time() + args.timeout_min * 60
    scaled_max = False
    while time.time() < deadline:
        try:
            http_post(f"{base}/api/stress/cluster/start", {"duration": 300})
        except Exception:
            pass
        asg = get_asg_info(args.asg_name, args.region)
        high_state, _ = get_alarm_state(args.high_alarm, args.region)
        in_service = [i for i in asg["Instances"] if i["LifecycleState"] == "InService"]
        print(f"   [Scale-Out] HighAlarm={high_state} | Desired={asg['DesiredCapacity']}/{asg['MaxSize']} | InService={len(in_service)}")
        if asg["DesiredCapacity"] >= asg["MaxSize"] and len(in_service) >= asg["MaxSize"]:
            scaled_max = True
            print(f"   ✅ ¡Alarma Scale-Out validada y ASG alcanzó {asg['MaxSize']} instancias InService!")
            break
        time.sleep(20)

    if not scaled_max:
        print("   ❌ El ASG no alcanzó el límite superior dentro del tiempo límite.")
        sys.exit(1)

    # 2. Fase Balanceo de Carga entre las instancias activas
    print("\n⚖️ [FASE 2] Validando balanceo de carga (32 peticiones)...")
    time.sleep(15)
    counts = Counter()
    for _ in range(32):
        try:
            res = http_get(f"{base}/api/whoami?t={time.time()}")
            counts[res["instance_id"]] += 1
        except Exception:
            pass
    for inst_id, cnt in counts.items():
        print(f"   • {inst_id}: {cnt} peticiones")
    if len(counts) < 2:
        print("   ❌ El ALB no distribuyó el tráfico entre múltiples instancias.")
        sys.exit(1)
    print(f"   ✅ Tráfico balanceado entre {len(counts)} instancias distintas.")

    # 3. Fase Scale In de regreso a min_size (1)
    print("\n🛑 [FASE 3] Deteniendo estrés en todo el clúster para validar Scale-In...")
    for _ in range(3):
        try:
            http_post(f"{base}/api/stress/cluster/stop")
        except Exception:
            pass
        time.sleep(2)

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
        print("   ❌ El ASG no regresó al mínimo dentro del tiempo límite.")
        sys.exit(1)

    print("\n🎉 VALIDACIÓN E2E COMPLETA (1 -> 4 -> 1) EXITOSA.")


if __name__ == "__main__":
    main()
