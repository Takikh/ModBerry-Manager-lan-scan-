#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="/etc/systemd/system/modberry_manager.service"
INSTALL_USER="${SUDO_USER:-mobilis}"

echo "=========================================="
echo "  MODBERRY MANAGER - INSTALLATION"
echo "=========================================="

echo "📦 Préparation du virtualenv..."
cd "$PROJECT_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "📦 Installation des dépendances système..."
apt-get update
apt-get install -y iproute2 openssh-client net-tools arp-scan || true

echo "📦 Écriture du service systemd..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=ModBerry Manager - LAN discovery service
After=network.target

[Service]
Type=simple
User=$INSTALL_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/app.py
Restart=always
RestartSec=10
Environment=MODBERRY_PORT=2310

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable modberry_manager.service
systemctl restart modberry_manager.service

echo ""
echo "=========================================="
echo "✅ INSTALLATION TERMINEE"
echo "=========================================="
echo ""
echo "URL: http://10.0.0.1:2310"
echo "Identifiant: admin-ip@modberry.local"
echo "Mot de passe: takieddine"
echo ""
echo "Service: systemctl status modberry_manager.service"
echo "Logs: journalctl -u modberry_manager.service -f"
