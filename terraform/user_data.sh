#!/bin/bash
set -euxo pipefail

dnf update -y
dnf install -y nginx python3 gzip python3-pip
pip3 install --no-cache-dir boto3 || dnf install -y python3-boto3

mkdir -p /opt/asg-demo

# Obtener metadatos de la instancia usando IMDSv2
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" || true)

get_meta() {
  local path="$1"
  if [ -n "$TOKEN" ]; then
    curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
      "http://169.254.169.254/latest/meta-data/$${path}" || echo "unknown"
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
  "instance_id": "$${INSTANCE_ID}",
  "az": "$${AZ}",
  "private_ip": "$${PRIVATE_IP}",
  "instance_type": "$${INSTANCE_TYPE}",
  "region": "${aws_region}",
  "asg_name": "${asg_name}",
  "high_alarm": "${high_alarm_name}",
  "low_alarm": "${low_alarm_name}",
  "target_group_arn": "${target_group_arn}",
  "app_version": "${app_version}"
}
EOF

# Desplegar API Python y Dashboard Nginx desde app/
echo "${server_py_gz_b64}" | base64 -d | gunzip > /opt/asg-demo/server.py
echo "${index_html_gz_b64}" | base64 -d | gunzip > /usr/share/nginx/html/index.html

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

# Configurar Nginx para proxy a /api/
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

systemctl enable --now nginx
systemctl restart nginx
