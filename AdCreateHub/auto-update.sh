#!/bin/bash
# AdCreateHub self-updater.
# Runs once a minute from /etc/cron.d/adcreatehub-update. Checks GitHub;
# if origin/main moved, waits for both apps to be idle, then pulls new code,
# refreshes Python deps, and restarts the hub. While an app is still running
# it touches .update-pending so the apps can show a "pending update" banner.
# After applying, it writes .update-log.json so the hub can show "✓ Updated".

set -e

# Self-locate so one script works for every clone path.
INNER="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$INNER/.." && pwd)"
VENV_PIP="$REPO_DIR/venv/bin/pip"
BRANCH="main"
LOG="/var/log/adcreatehub-update.log"
FLAG="$INNER/.update-pending"
LAST="$INNER/.update-log.json"

# user-writable state we must survive the reset
PRESERVE=(
  "AdCreateHub/megagen/config.json"
  "AdCreateHub/megagen/usage.json"
  "AdCreateHub/megagen/accounts.json"
  "AdCreateHub/Bananza/config.json"
)

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

# Is anything running? If so, defer and let the banner tell the user.
RUNNING=$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/internal/status 2>/dev/null \
  | grep -o '"any_running":true' || true)

if [ -n "$RUNNING" ]; then
  if [ ! -f "$FLAG" ]; then
    touch "$FLAG"
    echo "$(date -Iseconds) deferred: $LOCAL -> $REMOTE (apps running)" >> "$LOG"
  fi
  exit 0
fi

echo "$(date -Iseconds) update: $LOCAL -> $REMOTE" >> "$LOG"
rm -f "$FLAG"

# ---- preserve user config across reset ----
TMPD=$(mktemp -d)
for rel in "${PRESERVE[@]}"; do
  src="$REPO_DIR/$rel"
  if [ -f "$src" ]; then
    mkdir -p "$TMPD/$(dirname "$rel")"
    cp -p "$src" "$TMPD/$rel"
  fi
done

git reset --hard "origin/$BRANCH" >> "$LOG" 2>&1

for rel in "${PRESERVE[@]}"; do
  if [ -f "$TMPD/$rel" ]; then
    mkdir -p "$(dirname "$REPO_DIR/$rel")"
    cp -p "$TMPD/$rel" "$REPO_DIR/$rel"
  fi
done
rm -rf "$TMPD"

# refresh deps in case requirements.txt or hub needs changed
if [ -x "$VENV_PIP" ]; then
  "$VENV_PIP" install -q flask gradio -r "$INNER/megagen/requirements.txt" >> "$LOG" 2>&1 || true
fi

# ---- own everything back to the service user ----
# (git reset as root leaves root-owned files; app runs as ubuntu and must write)
SERVICE_USER=$(systemctl show adcreatehub-panel -p User --value 2>/dev/null | tr -d '[:space:]')
if [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != "root" ]; then
  chown -R "$SERVICE_USER:" "$REPO_DIR" 2>/dev/null || true
fi

# ---- stamp the "last applied" log so the hub can toast "✓ Updated" ----
SHORT_SHA=$(git rev-parse --short "$REMOTE")
SUBJECT=$(git log -1 --pretty=%s "$REMOTE" | tr -d '"' | cut -c1-200)
AT=$(date -Iseconds)
printf '{"sha":"%s","short":"%s","at":"%s","subject":"%s","from":"%s"}\n' \
  "$REMOTE" "$SHORT_SHA" "$AT" "$SUBJECT" "$LOCAL" > "$LAST"
chmod 644 "$LAST"
if [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != "root" ]; then
  chown "$SERVICE_USER:" "$LAST" 2>/dev/null || true
fi

systemctl restart adcreatehub-panel >> "$LOG" 2>&1
echo "$(date -Iseconds) restart ok ($SHORT_SHA)" >> "$LOG"
