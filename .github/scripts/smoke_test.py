#!/usr/bin/env python3
"""
Smoke test post-despliegue para validar que el ALB, Nginx y la API de
balanceo/estrés respondan correctamente.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def check_url(name, url, check_fn, max_retries=30, delay_sec=10):
    print(f"\n🔍 [PROBANDO] {name}: {url}")
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "GitHubActions-SmokeTest/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
                body = resp.read().decode("utf-8")
                if status == 200:
                    ok, msg = check_fn(body)
                    if ok:
                        print(f"   ✅ [EXITOSO] Intento {attempt}: {msg}")
                        return True
                    print(f"   ⚠️ Intento {attempt}/{max_retries}: {msg}")
                else:
                    print(f"   ⚠️ Intento {attempt}/{max_retries}: HTTP {status}")
        except urllib.error.HTTPError as e:
            print(f"   ⚠️ Intento {attempt}/{max_retries}: HTTP {e.code} - {e.reason}")
        except Exception as e:
            print(f"   ⚠️ Intento {attempt}/{max_retries}: {e}")

        if attempt < max_retries:
            time.sleep(delay_sec)

    print(f"   ❌ [FALLÓ] '{name}' no respondió correctamente.")
    return False


def wait_for_version(base, expected, max_wait_sec=900, needed_streak=6):
    """Espera hasta que TODAS las respuestas del ALB vengan de la versión nueva.

    Durante un instance refresh conviven instancias viejas y nuevas; exigimos
    varias respuestas seguidas con la versión esperada para descartar las viejas.
    """
    print(f"\n🔍 [PROBANDO] Versión desplegada == {expected}")
    deadline = time.time() + max_wait_sec
    streak = 0
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"{base}/api/health?t={time.time()}",
                headers={"User-Agent": "GitHubActions-SmokeTest/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            got = body.get("app_version")
            if got == expected:
                streak += 1
                if streak >= needed_streak:
                    print(f"   ✅ [EXITOSO] {needed_streak} respuestas seguidas con versión {expected}")
                    return True
            else:
                if streak:
                    print(f"   ⚠️ {body.get('instance_id')} aún sirve versión {got}")
                streak = 0
        except Exception as e:
            streak = 0
            print(f"   ⚠️ {e}")
        time.sleep(3 if streak else 10)
    print(f"   ❌ [FALLÓ] El ALB no sirvió solo la versión {expected} a tiempo.")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alb-url", required=True)
    parser.add_argument("--expected-version", default="")
    args = parser.parse_args()
    base = args.alb_url.rstrip("/")

    ok_html = check_url(
        "Página Nginx Principal",
        base,
        lambda b: ("AWS Auto Scaling" in b, "Dashboard Nginx cargado"),
        max_retries=36,
        delay_sec=10,
    )

    ok_version = True
    if args.expected_version:
        ok_version = wait_for_version(base, args.expected_version)

    ok_health = check_url(
        "Health Check API",
        f"{base}/api/health",
        lambda b: (
            json.loads(b).get("status") == "healthy",
            f"Health OK ({json.loads(b).get('instance_id')})",
        ),
        max_retries=10,
        delay_sec=5,
    )

    ok_whoami = check_url(
        "Endpoint WhoAmI (Balanceo)",
        f"{base}/api/whoami",
        lambda b: (
            "instance_id" in json.loads(b) and "cpu_percent" in json.loads(b),
            f"WhoAmI OK ({json.loads(b).get('instance_id')})",
        ),
        max_retries=10,
        delay_sec=5,
    )

    ok_cluster = check_url(
        "Endpoint Cluster Monitor (ASG + CloudWatch)",
        f"{base}/api/cluster",
        lambda b: (
            "asg" in json.loads(b) and "instances" in json.loads(b),
            f"Cluster OK (instancias: {len(json.loads(b).get('instances', []))})",
        ),
        max_retries=10,
        delay_sec=5,
    )

    if ok_version and ok_html and ok_health and ok_whoami and ok_cluster:
        print("\n🎉 ¡DESPLIEGUE VERIFICADO EXITOSAMENTE!")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
