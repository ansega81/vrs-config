#!/usr/bin/env bash
# Install the VRS wheel-base udev rules (root).
set -e
cd "$(dirname "$0")/.."
if [ "$(id -u)" != 0 ]; then echo "run with sudo"; exit 1; fi
install -m 0644 udev/99-vrs-dfp.rules /etc/udev/rules.d/99-vrs-dfp.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=change
udevadm trigger --subsystem-match=hidraw --action=change
echo "installed /etc/udev/rules.d/99-vrs-dfp.rules"
