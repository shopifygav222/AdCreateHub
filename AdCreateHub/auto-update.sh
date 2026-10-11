#!/bin/bash
# AdCreateHub self-updater.
# Runs once a minute from /etc/cron.d/adcreatehub-update. Fetches origin/main;
# if it moved, pulls, refreshes Python deps in the venv, and restarts the hub.
# Safe to run while nothing has changed — it exits fast when HEAD matches.

set -e

REPO_DIR="/home/ubuntu/adcreatehub"
VENV_PIP="$REPO_DIR/venv/bin/pip"
BRANCH="main"
LOG="/var/log/adcreatehub-update.log"

cd "$REPO_DIR"

# git needs to know who it's running as inside a cron environment
export HOME=/root
git config --global --add safe.directory "$REPO_DIR"

git fetch -q origin "$BRANCH"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

# fast path: nothing changed
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

echo "$(date -Iseconds) update: $LOCAL -> $REMOTE" >> "$LOG"

# wipe any local drift and match main exactly
git reset --hard "origin/$BRANCH" >> "$LOG" 2>&1

# refresh deps in case requirements.txt or hub needs changed
if [ -x "$VENV_PIP" ]; then
  "$VENV_PIP" install -q flask -r megagen/requirements.txt >> "$LOG" 2>&1 || true
fi

# bounce the service so the new code is live
systemctl restart adcreatehub-panel >> "$LOG" 2>&1

echo "$(date -Iseconds) restart ok" >> "$LOG"
