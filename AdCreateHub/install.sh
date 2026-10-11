#!/bin/bash
# AdCreateHub bootstrap installer.
# Run on a fresh Oracle Cloud Ubuntu VM as the `ubuntu` user:
#   curl -fsSL https://raw.githubusercontent.com/shopifygav222/AdCreateHub/main/AdCreateHub/install.sh | bash
#
# Idempotent — safe to re-run. Everything lives under ~/adcreatehub/.

set -e

REPO_URL="https://github.com/shopifygav222/AdCreateHub.git"
REPO_DIR="$HOME/adcreatehub"
INNER="$REPO_DIR/AdCreateHub"
VENV="$REPO_DIR/venv"

say() { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }

say "Installing system packages"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
  git python3 python3-venv python3-pip ffmpeg iptables-persistent curl ca-certificates

say "Cloning / updating the repo at $REPO_DIR"
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch -q origin main
  git -C "$REPO_DIR" reset --hard origin/main
else
  git clone -q "$REPO_URL" "$REPO_DIR"
fi

say "Building Python environment at $VENV"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade -q pip
"$VENV/bin/pip" install -q flask google-cloud-aiplatform \
  -r "$INNER/megagen/requirements.txt"

say "Opening firewall ports 8000 / 7860 / 7861"
for port in 8000 7860 7861; do
  if ! sudo iptables -C INPUT -p tcp --dport $port -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT -p tcp --dport $port -j ACCEPT
  fi
done
sudo netfilter-persistent save >/dev/null

say "Installing systemd service"
sudo tee /etc/systemd/system/adcreatehub-panel.service >/dev/null <<EOF
[Unit]
Description=AdCreateHub remote control panel
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$INNER
Environment=GOOGLE_APPLICATION_CREDENTIALS=$INNER/gcp-key.json
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python $INNER/controller.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q adcreatehub-panel

say "Installing auto-update cron (checks GitHub every minute)"
sudo tee /etc/cron.d/adcreatehub-update >/dev/null <<EOF
# AdCreateHub auto-update: pull origin/main and restart the hub when it moves.
*  *  *  *  *  root  $INNER/auto-update.sh
EOF
sudo chmod 644 /etc/cron.d/adcreatehub-update
sudo chmod +x "$INNER/auto-update.sh"
sudo touch /var/log/adcreatehub-update.log
sudo chmod 644 /var/log/adcreatehub-update.log

say "Starting the hub"
sudo systemctl restart adcreatehub-panel
sleep 2

IP=$(curl -fsSL --max-time 4 ifconfig.me 2>/dev/null || echo YOUR_VM_IP)

cat <<DONE

====================================================================
AdCreateHub is installed and running.

Open the hub:
    http://$IP:8000
    Password: minion01   (change it any time — see below)

Still to do (one time, outside the VM):

  1. In the Oracle Cloud web console open ports 8000 / 7860 / 7861
     (TCP, source 0.0.0.0/0) in your VCN's default security list.
     Without this the pages won't load from your phone.

  2. Sign into Google Cloud so the apps can generate:
         gcloud auth application-default login --no-launch-browser
     (Install gcloud first if needed: sudo snap install google-cloud-cli --classic)

Change the password:
    nano $INNER/controller.py        # edit the SECRET_KEY = "..." line
    sudo systemctl restart adcreatehub-panel

Watch what the auto-updater is doing:
    tail -f /var/log/adcreatehub-update.log

Check hub status:
    sudo systemctl status adcreatehub-panel
====================================================================
DONE
