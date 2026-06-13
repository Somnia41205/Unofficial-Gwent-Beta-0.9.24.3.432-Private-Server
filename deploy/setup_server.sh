#!/bin/bash
# Gwent Beta Private Server — Oracle Cloud / Ubuntu Server Setup Script
# Run as root (or with sudo) on a fresh Ubuntu 22.04+ ARM or x86 instance.
#
# Usage:
#   curl -sSL https://your-server.com/setup.sh | sudo bash
#   — or —
#   sudo bash setup_server.sh
#
# Prerequisites: Oracle Cloud instance with ports 443, 7777, 8445, 8447 open
# in the VCN security list (see SERVER_SETUP.md).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — edit these
# ---------------------------------------------------------------------------
GWENT_USER="gwent"
GWENT_DIR="/opt/gwent-server"
CERT_DIR="${GWENT_DIR}/certs"
DATA_DIR="${GWENT_DIR}/data"

# Auto-detect the public IP (Oracle Cloud metadata endpoint)
PUBLIC_IP=$(curl -s -H "Authorization: Bearer Oracle" \
  http://169.254.169.254/opc/v1/vnics/ 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['publicIp'])" 2>/dev/null \
  || curl -s ifconfig.me 2>/dev/null \
  || echo "REPLACE_WITH_YOUR_PUBLIC_IP")

echo "============================================="
echo "  Gwent Beta Private Server — Setup"
echo "  Detected public IP: ${PUBLIC_IP}"
echo "============================================="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip nginx openssl

# ---------------------------------------------------------------------------
# 2. Create user and directories
# ---------------------------------------------------------------------------
echo "[2/7] Creating gwent user and directories..."
id -u ${GWENT_USER} &>/dev/null || useradd -r -s /bin/false -m -d ${GWENT_DIR} ${GWENT_USER}
mkdir -p ${GWENT_DIR} ${CERT_DIR} ${DATA_DIR}

# ---------------------------------------------------------------------------
# 3. Generate self-signed TLS certificate
# ---------------------------------------------------------------------------
echo "[3/7] Generating TLS certificate..."
if [ ! -f "${CERT_DIR}/fake.crt" ]; then
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${CERT_DIR}/fake.key" \
        -out "${CERT_DIR}/fake.crt" \
        -days 3650 -nodes \
        -subj "/CN=*.gog.com" \
        -addext "subjectAltName=DNS:*.gog.com,DNS:gog.com"
    echo "  Certificate generated."
else
    echo "  Certificate already exists, skipping."
fi

# ---------------------------------------------------------------------------
# 4. Configure OS firewall (iptables — Oracle Cloud Ubuntu uses this)
# ---------------------------------------------------------------------------
echo "[4/7] Configuring firewall..."
# Oracle Cloud Ubuntu images have iptables rules that block non-SSH by default.
# We need to open ports BEFORE the REJECT rule.
for PORT in 443 7777 8445 8447; do
    if ! iptables -C INPUT -p tcp --dport ${PORT} -j ACCEPT 2>/dev/null; then
        iptables -I INPUT 1 -p tcp --dport ${PORT} -j ACCEPT
        echo "  Opened port ${PORT}"
    fi
done

# Persist iptables rules
if command -v netfilter-persistent &>/dev/null; then
    netfilter-persistent save
else
    apt-get install -y -qq iptables-persistent
    netfilter-persistent save
fi

# ---------------------------------------------------------------------------
# 5. Install nginx config
# ---------------------------------------------------------------------------
echo "[5/7] Configuring nginx..."
cat > /etc/nginx/sites-available/gwent <<'NGINX_EOF'
# Gwent Beta Private Server — nginx reverse proxy
# All seawolf-*.gog.com domains proxy to server.py on 127.0.0.1:8443

upstream gwent_backend {
    server 127.0.0.1:8443;
}

server {
    listen 443 ssl default_server;
    server_name seawolf-config.gog.com seawolf-deck.gog.com seawolf-inventory.gog.com
                seawolf-shop.gog.com seawolf-rankings.gog.com seawolf-profile.gog.com
                seawolf-rewards.gog.com seawolf-matchmaking.gog.com seawolf-games-log.gog.com
                remote-config.gog.com notifications-pusher.gog.com users.gog.com auth.gog.com;

    ssl_certificate     /opt/gwent-server/certs/fake.crt;
    ssl_certificate_key /opt/gwent-server/certs/fake.key;

    # Static files for shop catalog, card definitions, etc.
    # Adjust root path to where your static game data lives
    root /opt/gwent-server/static;

    # Proxy everything to the Python backend
    location / {
        proxy_pass https://gwent_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_ssl_verify off;
    }
}
NGINX_EOF

ln -sf /etc/nginx/sites-available/gwent /etc/nginx/sites-enabled/gwent
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ---------------------------------------------------------------------------
# 6. Create systemd service units
# ---------------------------------------------------------------------------
echo "[6/7] Creating systemd services..."

# Common environment file
cat > ${GWENT_DIR}/gwent.env <<EOF
GWENT_SERVER_IP=${PUBLIC_IP}
GWENT_DATA_DIR=${DATA_DIR}
GWENT_CERT_DIR=${CERT_DIR}
GWENT_USE_SQLITE=1
EOF

# server.py
cat > /etc/systemd/system/gwent-server.service <<EOF
[Unit]
Description=Gwent Beta API Server
After=network.target

[Service]
Type=simple
User=${GWENT_USER}
WorkingDirectory=${GWENT_DIR}
EnvironmentFile=${GWENT_DIR}/gwent.env
ExecStart=/usr/bin/python3 ${GWENT_DIR}/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# broker.py
cat > /etc/systemd/system/gwent-broker.service <<EOF
[Unit]
Description=Gwent Beta WebSocket Broker
After=network.target

[Service]
Type=simple
User=${GWENT_USER}
WorkingDirectory=${GWENT_DIR}
EnvironmentFile=${GWENT_DIR}/gwent.env
ExecStart=/usr/bin/python3 ${GWENT_DIR}/broker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# relay.py
cat > /etc/systemd/system/gwent-relay.service <<EOF
[Unit]
Description=Gwent Beta Game Relay
After=network.target

[Service]
Type=simple
User=${GWENT_USER}
WorkingDirectory=${GWENT_DIR}
EnvironmentFile=${GWENT_DIR}/gwent.env
ExecStart=/usr/bin/python3 ${GWENT_DIR}/relay.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Target to manage all services together
cat > /etc/systemd/system/gwent.target <<EOF
[Unit]
Description=Gwent Beta Private Server
Requires=gwent-server.service gwent-broker.service gwent-relay.service
After=gwent-server.service gwent-broker.service gwent-relay.service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable gwent-server gwent-broker gwent-relay gwent.target

# ---------------------------------------------------------------------------
# 7. Set ownership
# ---------------------------------------------------------------------------
echo "[7/7] Setting file ownership..."
chown -R ${GWENT_USER}:${GWENT_USER} ${GWENT_DIR}

echo ""
echo "============================================="
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Copy server files to ${GWENT_DIR}/"
echo "     (server.py, broker.py, relay.py, db.py,"
echo "      rewards.json, static game data, etc.)"
echo ""
echo "  2. If migrating from JSON, run:"
echo "     cd ${GWENT_DIR} && python3 db.py ${DATA_DIR}"
echo ""
echo "  3. Start all services:"
echo "     sudo systemctl start gwent.target"
echo ""
echo "  4. Check status:"
echo "     sudo systemctl status gwent-server gwent-broker gwent-relay"
echo ""
echo "  Public IP: ${PUBLIC_IP}"
echo "  Ports needed in VCN security list: 443, 7777, 8445, 8447"
echo "============================================="
