#!/usr/bin/env bash
# Install the SDSS udev rule so sdss-inputd can grab Sunshine's touch devices.
#
#   sudo ./packaging/install-udev-rule.sh
#
# /etc is writable on SteamOS (it is an overlay), so this does not touch the
# read-only rootfs. SteamOS 3.6+ drops unknown /etc changes on every OS update,
# so the rule is also registered on the atomic-update keep list.
set -euo pipefail

RULE="60-sdss-input.rules"
KEEP="sdss-atomic-update.conf"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dst="/etc/udev/rules.d/$RULE"
keep_dst="/etc/atomic-update.conf.d/$KEEP"

if [[ $EUID -ne 0 ]]; then
    echo "run me with sudo: sudo $0" >&2
    exit 1
fi

install -m 0644 "$here/$RULE" "$dst"

if [[ -d /etc/atomic-update.conf.d ]]; then
    install -m 0644 "$here/$KEEP" "$keep_dst"
    echo "installed $keep_dst (survives SteamOS updates)"
else
    echo "no /etc/atomic-update.conf.d — not a SteamOS 3.6+ image, skipping keep list" >&2
fi

udevadm control --reload-rules
# Existing devices keep their old permissions; Sunshine recreates them on the
# next session anyway, but retrigger so a running session picks them up too.
udevadm trigger --subsystem-match=input --action=change

echo "installed $dst"
echo "restart the SDSS session (or replug) so Sunshine recreates its devices"
