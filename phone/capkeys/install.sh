#!/bin/sh
# Run on the phone: sudo sh install.sh
set -e
d=$(dirname "$0")
install -m 755 "$d/capkeys.py" /usr/local/bin/capkeys.py
[ -e /etc/capkeys.conf ] || install -m 644 "$d/capkeys.conf" /etc/capkeys.conf
install -m 644 "$d/capkeys.service" /etc/systemd/system/capkeys.service
systemctl daemon-reload
systemctl enable capkeys.service
systemctl restart capkeys.service
systemctl --no-pager status capkeys.service | head -5
