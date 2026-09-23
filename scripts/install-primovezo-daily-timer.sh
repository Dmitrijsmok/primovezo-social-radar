#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="$USER_SYSTEMD_DIR/primovezo-social-radar.service"
TIMER="$USER_SYSTEMD_DIR/primovezo-social-radar.timer"

mkdir -p "$USER_SYSTEMD_DIR"

cat >"$SERVICE" <<EOF
[Unit]
Description=Primovezo Social Radar daily ecommerce lead scan
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=/bin/bash $ROOT/scripts/run-primovezo-daily.sh
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

cat >"$TIMER" <<'EOF'
[Unit]
Description=Run Primovezo Social Radar every morning

[Timer]
OnCalendar=*-*-* 09:00:00 Europe/Riga
Persistent=true
RandomizedDelaySec=10m
Unit=primovezo-social-radar.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now primovezo-social-radar.timer

echo
echo "Installed:"
echo "  $SERVICE"
echo "  $TIMER"
echo
systemctl --user list-timers primovezo-social-radar.timer --no-pager
echo
echo "If this account must run user timers while logged out, enable linger once:"
echo "  sudo loginctl enable-linger $USER"
