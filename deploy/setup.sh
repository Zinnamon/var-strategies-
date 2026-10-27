#!/usr/bin/env bash
# One-shot setup of varbot on a fresh Ubuntu 22.04 / 24.04 server (tested layout for
# Hetzner Cloud). Run as root:
#
#   curl -fsSL https://raw.githubusercontent.com/Zinnamon/var-strategies-/varbot/deploy/setup.sh | bash
#
# or clone the repo and run `bash deploy/setup.sh`. Safe to re-run: it updates the
# checkout and reinstalls without touching your .env.
#
# Environment overrides: REPO, BRANCH, APP_DIR.
set -euo pipefail

REPO="${REPO:-https://github.com/Zinnamon/var-strategies-.git}"
BRANCH="${BRANCH:-varbot}"
APP_DIR="${APP_DIR:-/opt/varbot}"
USER_NAME="varbot"

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/setup.sh)" >&2
  exit 1
fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git ufw

echo "== user and directories"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$APP_DIR/reports"
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"

echo "== code"
if [ -d "$APP_DIR/app/.git" ]; then
  sudo -u "$USER_NAME" git -C "$APP_DIR/app" fetch -q origin "$BRANCH"
  sudo -u "$USER_NAME" git -C "$APP_DIR/app" checkout -q "$BRANCH"
  sudo -u "$USER_NAME" git -C "$APP_DIR/app" pull -q origin "$BRANCH"
else
  sudo -u "$USER_NAME" git clone -q -b "$BRANCH" "$REPO" "$APP_DIR/app"
fi

echo "== python environment"
[ -d "$APP_DIR/venv" ] || sudo -u "$USER_NAME" python3 -m venv "$APP_DIR/venv"
sudo -u "$USER_NAME" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$USER_NAME" "$APP_DIR/venv/bin/pip" install -q -e "$APP_DIR/app"

echo "== configuration"
if [ ! -f "$APP_DIR/app/.env" ]; then
  sudo -u "$USER_NAME" cp "$APP_DIR/app/.env.example" "$APP_DIR/app/.env"
  chmod 600 "$APP_DIR/app/.env"
  echo "   created $APP_DIR/app/.env from the example; edit it next"
fi

echo "== systemd units"
for unit in varbot-scan.service varbot-scan.timer varbot-watch.service; do
  sed "s#__APP_DIR__#$APP_DIR#g" "$APP_DIR/app/deploy/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now varbot-scan.timer
systemctl enable varbot-watch.service

echo "== firewall (SSH only; the watch JSON endpoint stays on localhost)"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "== smoke test"
if sudo -u "$USER_NAME" "$APP_DIR/venv/bin/varbot" markets > /tmp/varbot-markets.txt 2>&1; then
  head -6 /tmp/varbot-markets.txt
  echo "   ok: $(grep -c PERP /tmp/varbot-markets.txt) crypto perps listed (full list: varbot markets)"
else
  cat /tmp/varbot-markets.txt
  echo "   markets check failed; see the error above"
fi

cat <<EOF

Done. Next:
  1. nano $APP_DIR/app/.env          # add keys and WATCH_SYMBOLS, then:
  2. systemctl start varbot-watch    # live books + Telegram alerts
  3. systemctl start varbot-scan     # run one scan now (the timer runs it daily at 07:00 UTC)
  4. journalctl -u varbot-watch -f   # follow the logs
Reports land in $APP_DIR/reports; the live JSON is at http://127.0.0.1:8080/ on the box.
EOF
