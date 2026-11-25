import concurrent.futures
import datetime
import json
import multiprocessing
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import boto3
except ImportError:
    boto3 = None

META_PATH = "/opt/asg-demo/meta.json"
with open(META_PATH, "r", encoding="utf-8") as f:
    META = json.load(f)

REGION = META.get("region", "us-east-1")
ASG_NAME = META.get("asg_name", "autoscaling-demo-asg")
HIGH_ALARM = META.get("high_alarm", "autoscaling-demo-high-cpu")
LOW_ALARM = META.get("low_alarm", "autoscaling-demo-low-cpu")
TG_ARN = META.get("target_group_arn", "")

cpu_percent = 0.0
stress_lock = threading.Lock()
stress_procs = []
stress_end_time = 0.0

cluster_cache_lock = threading.Lock()
cluster_cache_ts = 0.0
cluster_cache_data = None


def read_proc_stat():
    with open("/proc/stat", "r", encoding="utf-8") as f:
        parts = f.readline().split()
    values = [int(x) for x in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def cpu_sampler():
    global cpu_percent
    prev_idle, prev_total = read_proc_stat()
    while True:
        time.sleep(1.0)
        idle, total = read_proc_stat()
        d_idle = idle - prev_idle
        d_total = total - prev_total
        prev_idle, prev_total = idle, total
        if d_total > 0:
            cpu_percent = round(100.0 * (1.0 - (d_idle / d_total)), 1)


def cpu_burn_worker(stop_at):
    x = 0.0001
    while time.time() < stop_at:
        x = (x * 1.000001 + 0.000001) % 1000.0


def cleanup_finished_locked():
    global stress_procs, stress_end_time
    alive = [p for p in stress_procs if p.is_alive()]
    stress_procs = alive
    if not alive:
        stress_end_time = 0.0


def start_stress(duration_sec=240, workers=None):
    global stress_procs, stress_end_time
    duration_sec = max(10, min(int(duration_sec), 600))
    workers = max(2, os.cpu_count() or 2) if workers is None else max(1, min(int(workers), 8))

    with stress_lock:
        cleanup_finished_locked()
        for p in stress_procs:
            p.terminate()
        for p in stress_procs:
            p.join(timeout=1)
        stress_procs = []

        stop_at = time.time() + duration_sec
        stress_end_time = stop_at
        for _ in range(workers):
            p = multiprocessing.Process(target=cpu_burn_worker, args=(stop_at,), daemon=True)
            p.start()
            stress_procs.append(p)

    return get_status()


def stop_stress():
    global stress_procs, stress_end_time
    with stress_lock:
        for p in stress_procs:
            p.terminate()
        for p in stress_procs:
            p.join(timeout=1)
        stress_procs = []
        stress_end_time = 0.0
    return get_status()


def get_status():
    with stress_lock:
        cleanup_finished_locked()
        active = len(stress_procs) > 0
        remaining = max(0, int(stress_end_time - time.time())) if active else 0
        workers = len(stress_procs)
    return {
        "instance_id": META.get("instance_id", "unknown"),
        "az": META.get("az", "unknown"),
        "private_ip": META.get("private_ip", "unknown"),
        "instance_type": META.get("instance_type", "unknown"),
        "cpu_percent": cpu_percent,
        "stress_active": active,
        "stress_remaining_sec": remaining,
        "workers": workers,
        "timestamp": time.strftime("%H:%M:%S"),
    }


def probe_node(ip, method="GET", path="/api/whoami", payload=None):
    if not ip or ip == "unknown":
        return None
    url = f"http://{ip}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def get_cluster_status():
    global cluster_cache_ts, cluster_cache_data
    now = time.time()
    with cluster_cache_lock:
        if cluster_cache_data is not None and (now - cluster_cache_ts) < 3.0:
            return cluster_cache_data

    result = {
        "region": REGION,
        "asg": {
            "name": ASG_NAME,
            "min_size": 1,
            "max_size": 4,
            "desired_capacity": 0,
            "in_service_count": 0,
        },
        "instances": [],
        "alarms": [],
        "cpu_history": [],
        "activities": [],
        "error": None,
    }

    if boto3 is None:
        result["error"] = "boto3 no instalado"
        return result

    try:
        asg_client = boto3.client("autoscaling", region_name=REGION)
        ec2_client = boto3.client("ec2", region_name=REGION)
        cw_client = boto3.client("cloudwatch", region_name=REGION)
        elb_client = boto3.client("elbv2", region_name=REGION)

        groups = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME]).get("AutoScalingGroups", [])
        asg_instances = []
        if groups:
            g = groups[0]
            asg_instances = g.get("Instances", [])
            result["asg"] = {
                "name": g["AutoScalingGroupName"],
                "min_size": g["MinSize"],
                "max_size": g["MaxSize"],
                "desired_capacity": g["DesiredCapacity"],
                "in_service_count": sum(1 for i in asg_instances if i["LifecycleState"] == "InService"),
            }

        ids = [i["InstanceId"] for i in asg_instances]
        ec2_map = {}
        if ids:
            res_ec2 = ec2_client.describe_instances(InstanceIds=ids)
            for r in res_ec2.get("Reservations", []):
                for inst in r.get("Instances", []):
                    ec2_map[inst["InstanceId"]] = inst

        tg_health_map = {}
        if TG_ARN:
            th = elb_client.describe_target_health(TargetGroupArn=TG_ARN)
            for item in th.get("TargetHealthDescriptions", []):
                tg_health_map[item["Target"]["Id"]] = item["TargetHealth"]["State"]

        enriched = []
        for i in asg_instances:
            iid = i["InstanceId"]
            ec2_i = ec2_map.get(iid, {})
            priv_ip = ec2_i.get("PrivateIpAddress", "pending")
            enriched.append({
                "instance_id": iid,
                "az": i.get("AvailabilityZone", "-"),
                "instance_type": ec2_i.get("InstanceType", "t3.micro"),
                "private_ip": priv_ip,
                "public_ip": ec2_i.get("PublicIpAddress", "-"),
                "lifecycle_state": i.get("LifecycleState", "-"),
                "health_status": i.get("HealthStatus", "-"),
                "target_health": tg_health_map.get(iid, "not-registered"),
                "cpu_percent": None,
                "stress_active": False,
                "is_current": iid == META.get("instance_id"),
            })

        def _fill_node_metrics(item):
            if item["private_ip"] not in ("pending", "-", "unknown"):
                st = probe_node(item["private_ip"])
                if st:
                    item["cpu_percent"] = st.get("cpu_percent")
                    item["stress_active"] = st.get("stress_active", False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(_fill_node_metrics, enriched))

        result["instances"] = enriched

        alarms_resp = cw_client.describe_alarms(AlarmNames=[HIGH_ALARM, LOW_ALARM])
        for a in alarms_resp.get("MetricAlarms", []):
            result["alarms"].append({
                "name": a["AlarmName"],
                "state": a["StateValue"],
                "threshold": a.get("Threshold"),
                "operator": a.get("ComparisonOperator"),
                "reason": a.get("StateReason", ""),
                "updated": a["StateUpdatedTimestamp"].strftime("%H:%M:%S") if a.get("StateUpdatedTimestamp") else "-",
            })

        end_dt = datetime.datetime.now(datetime.timezone.utc)
        start_dt = end_dt - datetime.timedelta(minutes=15)
        metrics_resp = cw_client.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "AutoScalingGroupName", "Value": ASG_NAME}],
            StartTime=start_dt,
            EndTime=end_dt,
            Period=60,
            Statistics=["Average"],
        )
        dps = sorted(metrics_resp.get("Datapoints", []), key=lambda x: x["Timestamp"])
        result["cpu_history"] = [
            {
                "time": d["Timestamp"].astimezone().strftime("%H:%M"),
                "avg": round(d["Average"], 1),
            }
            for d in dps
        ]

        act_resp = asg_client.describe_scaling_activities(AutoScalingGroupName=ASG_NAME, MaxRecords=8)
        for act in act_resp.get("Activities", []):
            result["activities"].append({
                "time": act["StartTime"].strftime("%H:%M:%S"),
                "status": act["StatusCode"],
                "description": act.get("Description", ""),
                "cause": act.get("Cause", ""),
            })

    except Exception as e:
        result["error"] = str(e)

    with cluster_cache_lock:
        cluster_cache_ts = time.time()
        cluster_cache_data = result
    return result


def cluster_stress_action(action, duration=240):
    cluster = get_cluster_status()
    ips = {
        inst["private_ip"]
        for inst in cluster.get("instances", [])
        if inst.get("private_ip") not in (None, "pending", "-", "unknown")
    }
    if META.get("private_ip") not in (None, "unknown"):
        ips.add(META["private_ip"])

    path = "/api/stress/start" if action == "start" else "/api/stress/stop"
    payload = {"duration": duration} if action == "start" else {}

    def _hit(ip):
        if ip == META.get("private_ip"):
            return start_stress(duration) if action == "start" else stop_stress()
        return probe_node(ip, method="POST", path=path, payload=payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_hit, ips))

    return {
        "action": action,
        "targeted_nodes": len(ips),
        "responded": sum(1 for r in results if r is not None),
        "self": get_status(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            self._send_json(200, {"status": "healthy", "instance_id": META.get("instance_id")})
        elif path in ("/api/whoami", "/api/stress/status"):
            self._send_json(200, get_status())
        elif path == "/api/cluster":
            self._send_json(200, get_cluster_status())
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}

        if path == "/api/stress/start":
            self._send_json(200, start_stress(duration_sec=data.get("duration", 240), workers=data.get("workers")))
        elif path == "/api/stress/stop":
            self._send_json(200, stop_stress())
        elif path == "/api/stress/cluster/start":
            self._send_json(200, cluster_stress_action("start", duration=data.get("duration", 240)))
        elif path == "/api/stress/cluster/stop":
            self._send_json(200, cluster_stress_action("stop"))
        else:
            self._send_json(404, {"error": "Not found"})


if __name__ == "__main__":
    t = threading.Thread(target=cpu_sampler, daemon=True)
    t.start()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()
