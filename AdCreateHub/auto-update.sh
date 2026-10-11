#!/bin/bash
# AdCreateHub self-updater.
# Runs once a minute from /etc/cron.d/adcreatehub-update. Checks GitHub;
# if origin/main moved, waits for both apps to be idle, then pulls the new
# code, refreshes Python deps, and restarts the hub. While an app is still
# running, it touches .update-pending so Bananza and MegaGen can show a
# "pending update" banner inside their own UIs.

set -e

# Self-locate so one script works for every clone path.
INNER="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$INNER/.." && pwd)"
VENV_PIP="$REPO_DIR/venv/bin/pip"
BRANCH="main"
LOG="/var/log/adcreatehub-update.log"
FLAG="$INNER/.update-pending"

cd "$REPO_DIR"

# cron runs this as root; git refuses "unsafe" dirs owned by another user
export HOME=/root
git config --global --add safe.directory "$REPO_DIR"

git fetch -q origin "$BRANCH"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

# fast path: nothing new — clear any stale flag and exit
if [ "$LOCAL" = "$REMOTE" ]; then
  [ -f "$FLAG" ] && rm -f "$FLAG"
  exit 0
fi

# Is anything running on the hub right now? If yes, defer and let the banner
# tell the user. The hub exposes /api/internal/status with no auth, locally.
RUNNING=$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/internal/status 2>/dev/null \
  | grep -o '"any_running":true' || true)

if [ -n "$RUNNING" ]; then
  if [ ! -f "$FLAG" ]; then
    touch "$FLAG"
    echo "$(date -Iseconds) deferred: $LOCAL -> $REMOTE (apps running)" >> "$LOG"
  fi
  exit 0
fi

# Nothing running — apply the update.
echo "$(date -Iseconds) update: $LOCAL -> $REMOTE" >> "$LOG"
rm -f "$FLAG"
git reset --hard "origin/$BRANCH" >> "$LOG" 2>&1

# refresh deps in case requirements.txt or hub needs changed
if [ -x "$VENV_PIP" ]; then
  "$VENV_PIP" install -q flask gradio -r "$INNER/megagen/requirements.txt" >> "$LOG" 2>&1 || true
fi

systemctl restart adcreatehub-panel >> "$LOG" 2>&1
echo "$(date -Iseconds) restart ok" >> "$LOG"
