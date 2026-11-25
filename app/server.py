import json
import multiprocessing
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

META_PATH = "/opt/asg-demo/meta.json"
with open(META_PATH, "r", encoding="utf-8") as f:
    META = json.load(f)

cpu_percent = 0.0
stress_lock = threading.Lock()
stress_procs = []
stress_end_time = 0.0


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


def start_stress(duration_sec=180, workers=None):
    global stress_procs, stress_end_time
    duration_sec = max(10, min(int(duration_sec), 600))
    if workers is None:
        workers = max(2, os.cpu_count() or 2)
    else:
        workers = max(1, min(int(workers), 8))

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
            res = start_stress(
                duration_sec=data.get("duration", 180),
                workers=data.get("workers"),
            )
            self._send_json(200, res)
        elif path == "/api/stress/stop":
            res = stop_stress()
            self._send_json(200, res)
        else:
            self._send_json(404, {"error": "Not found"})


if __name__ == "__main__":
    t = threading.Thread(target=cpu_sampler, daemon=True)
    t.start()
    server = ThreadingHTTPServer(("127.0.0.1", 8080), Handler)
    server.serve_forever()
