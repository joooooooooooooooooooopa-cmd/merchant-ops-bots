#!/usr/bin/env bash
# Создаёт systemd unit `reportbot` на ВПС. Запускать один раз.
set -euo pipefail
HOST="${HOST:?set HOST=user@host}"
KEY="${KEY:-$HOME/.ssh/ops_vps}"

ssh -i "$KEY" "$HOST" "sudo tee /etc/systemd/system/reportbot.service >/dev/null" <<'UNIT'
[Unit]
Description=ReportBot — private Excel reports bot for owner
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ReportBot
EnvironmentFile=/home/ubuntu/ReportBot/.env
ExecStart=/home/ubuntu/ReportBot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

ssh -i "$KEY" "$HOST" "sudo systemctl daemon-reload && sudo systemctl enable reportbot"
echo "✅ unit created and enabled. Now: sudo systemctl start reportbot"
