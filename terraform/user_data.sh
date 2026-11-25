#!/bin/bash
set -euxo pipefail

dnf update -y
dnf install -y nginx python3

mkdir -p /opt/asg-demo

# Obtener metadatos de la instancia usando IMDSv2
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" || true)

get_meta() {
  local path="$1"
  if [ -n "$TOKEN" ]; then
    curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
      "http://169.254.169.254/latest/meta-data/${path}" || echo "unknown"
  else
    echo "unknown"
  fi
}

INSTANCE_ID=$(get_meta "instance-id")
AZ=$(get_meta "placement/availability-zone")
PRIVATE_IP=$(get_meta "local-ipv4")
INSTANCE_TYPE=$(get_meta "instance-type")

cat <<EOF > /opt/asg-demo/meta.json
{
  "instance_id": "${INSTANCE_ID}",
  "az": "${AZ}",
  "private_ip": "${PRIVATE_IP}",
  "instance_type": "${INSTANCE_TYPE}"
}
EOF

# Servicio Python ligero para métricas de CPU, WhoAmI y Prueba de Estrés
cat <<'PYEOF' > /opt/asg-demo/server.py
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
PYEOF

cat <<'EOF' > /etc/systemd/system/asg-demo-api.service
[Unit]
Description=ASG Demo Python API
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/asg-demo/server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now asg-demo-api

# Configurar Nginx para servir la app y hacer proxy a /api/
cat <<'EOF' > /etc/nginx/default.d/asg-demo.conf
location /api/ {
    proxy_pass http://127.0.0.1:8080/api/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    keepalive_timeout 0;
    add_header Cache-Control "no-store, no-cache, must-revalidate" always;
}
EOF

# Página web visual en Nginx
cat <<'HTMLEOF' > /usr/share/nginx/html/index.html
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AWS Auto Scaling & Load Balancing Demo</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --green: #22c55e;
      --red: #ef4444;
      --amber: #f59e0b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 24px;
      line-height: 1.5;
    }
    .container { max-width: 1100px; margin: 0 auto; }
    header {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    h1 { font-size: 1.6rem; color: var(--accent); }
    .badge {
      background: #0284c733;
      border: 1px solid var(--accent);
      color: var(--accent);
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 0.85rem;
      font-weight: 600;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 20px;
      margin-bottom: 24px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
    }
    .card h2 {
      font-size: 1.15rem;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .meta-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .meta-item {
      background: #0f172a;
      padding: 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .meta-label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; }
    .meta-val { font-size: 1rem; font-weight: 700; margin-top: 4px; word-break: break-all; }
    .cpu-bar-bg {
      width: 100%;
      height: 16px;
      background: #0f172a;
      border-radius: 999px;
      overflow: hidden;
      margin: 12px 0;
      border: 1px solid var(--border);
    }
    .cpu-bar-fill {
      height: 100%;
      width: 0%;
      background: var(--green);
      transition: width 0.4s ease, background 0.4s ease;
    }
    .btn-row { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    button {
      border: none;
      border-radius: 8px;
      padding: 10px 16px;
      font-size: 0.92rem;
      font-weight: 600;
      cursor: pointer;
      color: #fff;
      transition: opacity 0.2s, transform 0.1s;
    }
    button:hover { opacity: 0.9; }
    button:active { transform: scale(0.98); }
    .btn-danger { background: var(--red); }
    .btn-warn { background: var(--amber); color: #0f172a; }
    .btn-primary { background: #0284c7; }
    .btn-success { background: #16a34a; }
    .btn-secondary { background: #475569; }
    .info-text { font-size: 0.88rem; color: var(--muted); margin-top: 10px; }
    .lb-bars { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; }
    .lb-row {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
    }
    .lb-row-top {
      display: flex;
      justify-content: space-between;
      font-size: 0.9rem;
      font-weight: 600;
      margin-bottom: 6px;
    }
    .lb-track {
      height: 12px;
      background: #1e293b;
      border-radius: 999px;
      overflow: hidden;
    }
    .lb-fill {
      height: 100%;
      border-radius: 999px;
      transition: width 0.3s ease;
    }
    .log-box {
      margin-top: 14px;
      background: #020617;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      height: 170px;
      overflow-y: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.8rem;
      color: #cbd5e1;
    }
    .status-pill {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 700;
    }
    .pill-ok { background: #16a34a33; color: #4ade80; border: 1px solid #22c55e; }
    .pill-hot { background: #ef444433; color: #f87171; border: 1px solid #ef4444; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>🚀 AWS Auto Scaling &amp; Load Balancing (Nginx)</h1>
        <p class="info-text">Demostración visual de escalamiento automático (1 → 4 instancias t3.micro) y balanceo de carga ALB</p>
      </div>
      <div class="badge">t3.micro · Min: 1 · Max: 4 · Capa Gratuita</div>
    </header>

    <div class="card" style="margin-bottom: 24px;">
      <h2>🖥️ Instancia Respondiendo Actualmente</h2>
      <div class="meta-grid">
        <div class="meta-item">
          <div class="meta-label">Instance ID</div>
          <div class="meta-val" id="cur-id">Cargando...</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Availability Zone</div>
          <div class="meta-val" id="cur-az">-</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">IP Privada</div>
          <div class="meta-val" id="cur-ip">-</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">CPU Actual</div>
          <div class="meta-val" id="cur-cpu">0%</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Estado Estrés</div>
          <div class="meta-val" id="cur-stress"><span class="status-pill pill-ok">Inactivo</span></div>
        </div>
      </div>
      <div class="cpu-bar-bg">
        <div class="cpu-bar-fill" id="cpu-bar"></div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>🔥 1. Prueba de Estrés CPU (Escalamiento)</h2>
        <p class="info-text">
          Al presionar el botón, se genera carga de CPU (~95%+) para activar la alarma de CloudWatch
          (<strong>CPU &ge; 50% durante 2 min</strong>) y escalar el ASG hacia el límite superior de <strong>4 instancias</strong>.
          Al detenerlo, la alarma de baja CPU (<strong>&le; 20% durante 2 min</strong>) reduce el grupo a <strong>1 instancia</strong>.
        </p>
        <div class="btn-row">
          <button class="btn-danger" onclick="startMultiStress()">🔥 Iniciar Estrés Multi-Instancia</button>
          <button class="btn-warn" onclick="startSingleStress()">⚡ Estrés 1 Petición</button>
          <button class="btn-secondary" onclick="stopAllStress()">🛑 Detener Estrés</button>
        </div>
        <p class="info-text" id="stress-msg">Listo para iniciar prueba de estrés.</p>
      </div>

      <div class="card">
        <h2>⚖️ 2. Prueba Visual de Balanceo de Carga</h2>
        <p class="info-text">
          Envía múltiples peticiones HTTP a través del <strong>Application Load Balancer</strong> y muestra en tiempo real
          qué instancias EC2 están respondiendo y cómo se distribuye el tráfico.
        </p>
        <div class="btn-row">
          <button class="btn-primary" onclick="runLoadBalanceBurst(24)">⚖️ Probar Balanceo (24 peticiones)</button>
          <button class="btn-success" id="btn-live-lb" onclick="toggleLiveLB()">🔄 Iniciar Monitor Continuo</button>
          <button class="btn-secondary" onclick="resetLBStats()">🧹 Limpiar</button>
        </div>
        <div class="lb-bars" id="lb-bars">
          <p class="info-text">Presiona "Probar Balanceo" para visualizar la distribución entre instancias.</p>
        </div>
        <div class="log-box" id="lb-log"></div>
      </div>
    </div>
  </div>

  <script>
    const PALETTE = ['#38bdf8', '#22c55e', '#f59e0b', '#a855f7', '#ec4899', '#14b8a6'];
    const instanceStats = {};
    let totalRequests = 0;
    let multiStressTimer = null;
    let liveLbTimer = null;

    function colorForInstance(id) {
      const keys = Object.keys(instanceStats);
      const idx = keys.indexOf(id);
      return PALETTE[(idx >= 0 ? idx : 0) % PALETTE.length];
    }

    function updateCurrentCard(data) {
      document.getElementById('cur-id').textContent = data.instance_id;
      document.getElementById('cur-az').textContent = data.az;
      document.getElementById('cur-ip').textContent = data.private_ip;
      document.getElementById('cur-cpu').textContent = `${data.cpu_percent}%`;

      const bar = document.getElementById('cpu-bar');
      bar.style.width = `${Math.min(100, data.cpu_percent)}%`;
      bar.style.background = data.cpu_percent >= 50 ? '#ef4444' : (data.cpu_percent >= 25 ? '#f59e0b' : '#22c55e');

      const pill = document.getElementById('cur-stress');
      if (data.stress_active) {
        pill.innerHTML = `<span class="status-pill pill-hot">Activo (${data.stress_remaining_sec}s)</span>`;
      } else {
        pill.innerHTML = `<span class="status-pill pill-ok">Inactivo</span>`;
      }
    }

    async function fetchWhoami() {
      const res = await fetch(`/api/whoami?t=${Date.now()}_${Math.random()}`, { cache: 'no-store' });
      const data = await res.json();
      updateCurrentCard(data);
      return data;
    }

    async function startSingleStress() {
      const res = await fetch('/api/stress/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: 240 })
      });
      const data = await res.json();
      updateCurrentCard(data);
      document.getElementById('stress-msg').textContent =
        `⚡ Estrés enviado a ${data.instance_id} (CPU: ${data.cpu_percent}%).`;
    }

    function startMultiStress() {
      if (multiStressTimer) clearInterval(multiStressTimer);
      document.getElementById('stress-msg').textContent =
        '🔥 Modo Estrés Multi-Instancia ACTIVO: enviando carga periódica a todas las instancias del ALB para escalar hasta 4...';
      const sendBurst = async () => {
        for (let i = 0; i < 4; i++) {
          fetch('/api/stress/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ duration: 240 })
          }).then(r => r.json()).then(updateCurrentCard).catch(() => {});
        }
      };
      sendBurst();
      multiStressTimer = setInterval(sendBurst, 5000);
    }

    async function stopAllStress() {
      if (multiStressTimer) {
        clearInterval(multiStressTimer);
        multiStressTimer = null;
      }
      document.getElementById('stress-msg').textContent = '🛑 Deteniendo estrés en todas las instancias...';
      for (let i = 0; i < 16; i++) {
        fetch('/api/stress/stop', { method: 'POST' })
          .then(r => r.json())
          .then(updateCurrentCard)
          .catch(() => {});
      }
      setTimeout(() => {
        document.getElementById('stress-msg').textContent =
          '✅ Estrés detenido. Cuando el CPU promedio baje de 20% por 2 min, el ASG escalará hacia abajo.';
      }, 1000);
    }

    function recordLBHit(data) {
      totalRequests++;
      if (!instanceStats[data.instance_id]) {
        instanceStats[data.instance_id] = {
          count: 0,
          az: data.az,
          ip: data.private_ip,
          cpu: data.cpu_percent,
          stress: data.stress_active
        };
      }
      instanceStats[data.instance_id].count++;
      instanceStats[data.instance_id].cpu = data.cpu_percent;
      instanceStats[data.instance_id].stress = data.stress_active;

      renderLBBars();

      const logBox = document.getElementById('lb-log');
      const line = document.createElement('div');
      line.textContent = `[${data.timestamp}] #${totalRequests} -> ${data.instance_id} (${data.az}) | CPU: ${data.cpu_percent}%`;
      logBox.prepend(line);
      if (logBox.children.length > 60) {
        logBox.removeChild(logBox.lastChild);
      }
    }

    function renderLBBars() {
      const container = document.getElementById('lb-bars');
      const ids = Object.keys(instanceStats);
      if (ids.length === 0) {
        container.innerHTML = '<p class="info-text">Sin datos aún.</p>';
        return;
      }
      container.innerHTML = ids.map(id => {
        const st = instanceStats[id];
        const pct = totalRequests > 0 ? Math.round((st.count / totalRequests) * 100) : 0;
        const color = colorForInstance(id);
        return `
          <div class="lb-row">
            <div class="lb-row-top">
              <span style="color:${color}">${id} (${st.az} · ${st.ip})</span>
              <span>${st.count} reqs (${pct}%) · CPU ${st.cpu}%</span>
            </div>
            <div class="lb-track">
              <div class="lb-fill" style="width:${pct}%; background:${color};"></div>
            </div>
          </div>
        `;
      }).join('');
    }

    async function runLoadBalanceBurst(n) {
      for (let i = 0; i < n; i++) {
        try {
          const data = await fetchWhoami();
          recordLBHit(data);
        } catch (e) {}
      }
    }

    function toggleLiveLB() {
      const btn = document.getElementById('btn-live-lb');
      if (liveLbTimer) {
        clearInterval(liveLbTimer);
        liveLbTimer = null;
        btn.textContent = '🔄 Iniciar Monitor Continuo';
      } else {
        btn.textContent = '⏸️ Pausar Monitor Continuo';
        liveLbTimer = setInterval(async () => {
          try {
            const data = await fetchWhoami();
            recordLBHit(data);
          } catch (e) {}
        }, 800);
      }
    }

    function resetLBStats() {
      for (const k of Object.keys(instanceStats)) delete instanceStats[k];
      totalRequests = 0;
      document.getElementById('lb-log').innerHTML = '';
      renderLBBars();
    }

    fetchWhoami().catch(() => {});
    setInterval(() => fetchWhoami().catch(() => {}), 3000);
  </script>
</body>
</html>
HTMLEOF

systemctl enable --now nginx
systemctl restart nginx
