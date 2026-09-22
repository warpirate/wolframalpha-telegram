#!/usr/bin/env bash
# Pull the latest code and restart the bot.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/prep-bot}"
SERVICE="prep-bot"

cd "$APP_DIR"
echo "==> Pulling"
git pull --ff-only
echo "==> Updating dependencies"
./.venv/bin/pip install --quiet -r requirements.txt
echo "==> Restarting"
sudo systemctl restart "$SERVICE"
sleep 3
sudo systemctl status "$SERVICE" --no-pager --lines=10
