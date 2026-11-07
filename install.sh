#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Execute como root: sudo bash install.sh"
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
id serverpulse >/dev/null 2>&1 || useradd --system --home /opt/serverpulse --shell /usr/sbin/nologin serverpulse
install -d -o root -g serverpulse -m 0750 /opt/serverpulse
install -d -o root -g serverpulse -m 0750 /opt/serverpulse/static
install -o root -g serverpulse -m 0640 "$HERE/serverpulse.py" /opt/serverpulse/serverpulse.py
install -o root -g serverpulse -m 0640 "$HERE/static/index.html" /opt/serverpulse/static/index.html
install -o root -g root -m 0644 "$HERE/systemd/serverpulse.service" /etc/systemd/system/serverpulse.service
systemctl daemon-reload
systemctl enable --now serverpulse
echo
echo "ServerPulse instalado."
echo "Estado: systemctl status serverpulse"
echo "Logs:   journalctl -u serverpulse -f"
echo "Teste:  curl http://127.0.0.1:8764/api/health"
echo "O serviço escuta somente em 127.0.0.1; publique pelo Caddy com autenticação."
