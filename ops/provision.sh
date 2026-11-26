#!/usr/bin/env bash
# Idempotent provisioning for a Debian 12 / Ubuntu 22.04+ VM (GCP e2-micro).
# Usage (as root):  bash ops/provision.sh [REPO_URL] [BRANCH]
# Safe to re-run: every step checks before it changes anything.
set -euo pipefail

REPO_URL="${1:-https://github.com/zhandosayupov/job-pipeline.git}"
BRANCH="${2:-main}"
APP_DIR=/opt/jobpipe
HOME_DIR=/var/lib/jobpipe
ETC_DIR=/etc/jobpipe
SVC_USER=jobpipe
UV_VERSION=0.13.8         # pinned; bump deliberately (keep in step with .github/workflows/ci.yml)
PYTHON_VERSION=3.12
SWAP_SIZE=2G

log() { printf '\n==> %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git sqlite3 curl ca-certificates >/dev/null

log "swap ($SWAP_SIZE)"
if ! swapon --show=NAME --noheadings | grep -q '^/swapfile$'; then
  [[ -f /swapfile ]] || { fallocate -l "$SWAP_SIZE" /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; }
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -q vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/90-jobpipe.conf

log "journald size limit"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/jobpipe.conf <<'EOF'
[Journal]
SystemMaxUse=200M
MaxRetentionSec=30day
EOF
systemctl restart systemd-journald

log "service user"
id "$SVC_USER" &>/dev/null || useradd --system --home-dir "$HOME_DIR" --create-home --shell /usr/sbin/nologin "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$HOME_DIR"

log "uv $UV_VERSION"
UV=/usr/local/bin/uv
if ! [[ -x $UV ]] || ! "$UV" --version | grep -q "$UV_VERSION"; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

log "code in $APP_DIR ($BRANCH)"
if [[ -d $APP_DIR/.git ]]; then
  sudo -u "$SVC_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$SVC_USER" git -C "$APP_DIR" checkout --quiet "$BRANCH"
  sudo -u "$SVC_USER" git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
  install -d -o "$SVC_USER" -g "$SVC_USER" "$APP_DIR"
  sudo -u "$SVC_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

log "python env"
sudo -u "$SVC_USER" env UV_PYTHON_INSTALL_DIR="$HOME_DIR/.uv-python" UV_CACHE_DIR="$HOME_DIR/.uv-cache" \
  "$UV" sync --project "$APP_DIR" --locked --no-dev --python "$PYTHON_VERSION" --quiet

log "secrets file"
install -d -m 750 -o root -g "$SVC_USER" "$ETC_DIR"
if [[ ! -f $ETC_DIR/secrets.env ]]; then
  install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$APP_DIR/ops/secrets.env.example" "$ETC_DIR/secrets.env"
  echo "   created $ETC_DIR/secrets.env: fill it in (sudo -u $SVC_USER nano $ETC_DIR/secrets.env)"
fi
chown "$SVC_USER:$SVC_USER" "$ETC_DIR/secrets.env"; chmod 600 "$ETC_DIR/secrets.env"

log "JOBPIPE_HOME"
sudo -u "$SVC_USER" env JOBPIPE_HOME="$HOME_DIR" JOBPIPE_SECRETS="$ETC_DIR/secrets.env" \
  "$APP_DIR/.venv/bin/jobpipe" init

log "systemd units"
for unit in "$APP_DIR"/ops/systemd/*.{service,timer}; do
  install -m 644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable --now jobpipe-queue.timer jobpipe-backup.timer

if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ETC_DIR/secrets.env" \
   && grep -qE '^\s*owner_id:\s*[0-9]+' "$HOME_DIR/config/settings.yaml"; then
  systemctl enable jobpipe-bot.service
  systemctl restart jobpipe-bot.service
  log "bot (re)started"
else
  log "bot NOT started: set TELEGRAM_BOT_TOKEN in $ETC_DIR/secrets.env and telegram.owner_id in $HOME_DIR/config/settings.yaml, then re-run"
fi

log "health"
sleep 2
curl -fsS http://127.0.0.1:8080/healthz && echo || echo "   /healthz not answering (bot not running yet?)"
systemctl list-timers 'jobpipe-*' --no-pager
