#!/usr/bin/env bash
# Bootstrap the prep bot on a fresh Ubuntu VM (Oracle Cloud Always Free, or any
# other Ubuntu box). Safe to re-run: it updates the checkout and restarts.
#
#   curl -fsSL https://raw.githubusercontent.com/warpirate/wolframalpha-telegram/main/deploy/setup.sh | bash
#
# or, after cloning:  bash deploy/setup.sh

set -euo pipefail

REPO="https://github.com/warpirate/wolframalpha-telegram.git"
APP_DIR="$HOME/prep-bot"
SERVICE="prep-bot"
PY=python3

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- packages
log "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git

# ------------------------------------------------------------------- swap
# Always Free AMD micro instances have 1 GB RAM. Pillow resizing a large photo
# can spike; a small swap file stops the OOM killer from reaping the bot.
if ! sudo swapon --show | grep -q '/swapfile'; then
    log "Creating a 1 GB swap file"
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
    log "Swap already configured, skipping"
fi

# ------------------------------------------------------------------- code
if [ -d "$APP_DIR/.git" ]; then
    log "Updating existing checkout in $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
else
    log "Cloning into $APP_DIR"
    git clone --depth 1 "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

# --------------------------------------------------------------- virtualenv
log "Setting up the virtualenv"
[ -d .venv ] || $PY -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

# --------------------------------------------------------------------- env
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    warn "Created .env from the template. It still holds placeholder values."
    warn "Edit it now, then re-run this script:"
    warn "    nano $APP_DIR/.env"
    warn "You need TELEGRAM_BOT_TOKEN and NEBIUS_API_KEY."
    exit 1
fi
chmod 600 .env

if grep -qE '^(TELEGRAM_BOT_TOKEN|NEBIUS_API_KEY)=your_' .env; then
    warn ".env still contains placeholder values. Edit it, then re-run:"
    warn "    nano $APP_DIR/.env"
    exit 1
fi

# ----------------------------------------------------------------- service
log "Installing the systemd service"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=TSLPRB Prep Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Basic hardening - the bot only needs its own directory and outbound network.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE

sleep 4
log "Status"
sudo systemctl status $SERVICE --no-pager --lines=15 || true

cat <<EOF

--------------------------------------------------------------------
Done. The bot runs now and starts automatically on every reboot.

  Live logs     journalctl -u $SERVICE -f
  Restart       sudo systemctl restart $SERVICE
  Stop          sudo systemctl stop $SERVICE
  Update code   bash $APP_DIR/deploy/update.sh

Remember: only ONE instance may poll per bot token. Stop the bot on
your laptop (stop_bot.bat and uninstall_autostart.bat) or both will
fail with "Conflict: terminated by other getUpdates".
--------------------------------------------------------------------
EOF
