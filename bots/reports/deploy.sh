#!/usr/bin/env bash
# Деплой ReportBot на ВПС (HOST=user@host).
#
# Pre-req на ВПС (один раз):
#   sudo mkdir -p /home/ubuntu/ReportBot
#   sudo chown ubuntu:ubuntu /home/ubuntu/ReportBot
#   # venv (uv, Python 3.12 — как у других ботов):
#   cd /home/ubuntu/ReportBot
#   ~/.local/bin/uv venv -p 3.12 .venv
#   VIRTUAL_ENV=$(pwd)/.venv ~/.local/bin/uv pip install -r requirements.txt
#   # systemd unit: см. systemd_unit.sh ниже.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:?set HOST=user@host}"
KEY="${KEY:-$HOME/.ssh/ops_vps}"
DEST="${DEST:-/home/ubuntu/ReportBot}"
SERVICE="${SERVICE:-reportbot}"

echo "→ syncing code to $HOST:$DEST"
rsync -az -e "ssh -i $KEY" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
  --exclude '*.json' --exclude '*.log' --exclude '.git' \
  --delete-after \
  bot.py config.py db.py builder.py keys.py \
  requirements.txt start.sh \
  "$HOST:$DEST/"

# handlers/ — слать ОТДЕЛЬНО в подпапку (rsync handlers/ в корень ложит в корень — баг CRM-бота)
rsync -az -e "ssh -i $KEY" \
  --exclude '__pycache__' --delete-after \
  handlers/ "$HOST:$DEST/handlers/"

echo "→ installing deps"
ssh -i "$KEY" "$HOST" "cd $DEST && VIRTUAL_ENV=\$(pwd)/.venv ~/.local/bin/uv pip install -r requirements.txt -q"

echo "→ restarting $SERVICE"
ssh -i "$KEY" "$HOST" "sudo systemctl restart $SERVICE && sleep 1 && systemctl is-active $SERVICE"

echo "✅ deployed. Logs: ssh $HOST 'journalctl -u $SERVICE -f'"
