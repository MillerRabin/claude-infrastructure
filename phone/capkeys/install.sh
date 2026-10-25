#!/bin/sh
# Run on the phone: sudo sh ~/capkeys/install.sh
set -e
d=$(dirname "$0")
install -m 755 "$d/capkeys.py" /usr/local/bin/capkeys.py
install -m 644 "$d/capkeys.service" /etc/systemd/system/capkeys.service
systemctl daemon-reload
systemctl enable --now capkeys.service
systemctl --no-pager status capkeys.service | head -5
