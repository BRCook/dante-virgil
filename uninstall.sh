#!/bin/bash
# Remove Virgil from this machine, and give timekeeping back to Debian's chrony.
#
#   sudo ./uninstall.sh           keeps the config in /etc/dante-virgil
#   sudo ./uninstall.sh --purge   removes the config and the dante-virgil user too
#
# The Debian packages Virgil installed (chrony, linuxptp and so on) are left alone.

set -euo pipefail

PREFIX=/opt/dante-virgil
SERVICE_USER=dante-virgil
CONFIG_DIR=/etc/dante-virgil
UNIT_DIR=/usr/local/lib/systemd/system
TMPFILES_DIR=/usr/local/lib/tmpfiles.d
POLKIT_DIR=/etc/polkit-1/rules.d
APPARMOR_PROFILE=/etc/apparmor.d/usr.sbin.chronyd
APPARMOR_LOCAL=/etc/apparmor.d/local/usr.sbin.chronyd

[[ $EUID -eq 0 ]] || { echo "uninstall.sh: run this with sudo" >&2; exit 1; }
purge=false
[[ "${1:-}" == --purge ]] && purge=true

echo "Stopping Virgil"
systemctl disable --now dante-virgil.service 2>/dev/null || true
for unit in dante-virgil-netaudio dante-virgil-phc2sys dante-virgil-ptp4l dante-virgil-chrony; do
    systemctl stop "$unit.service" 2>/dev/null || true
done

echo "Removing Virgil's files"
rm -f "$UNIT_DIR"/dante-virgil.service "$UNIT_DIR"/dante-virgil-*.service "$TMPFILES_DIR/dante-virgil.conf" \
    "$POLKIT_DIR/50-dante-virgil.rules" /usr/local/bin/virgilctl
rm -rf "$PREFIX" /run/dante-virgil /run/dante-virgil-generated /var/lib/dante-virgil
systemctl daemon-reload

if [[ -e "$APPARMOR_LOCAL" ]]; then
    echo "Removing Virgil's AppArmor rule"
    sed -i -e '\|^# Added by Virgil$|d' -e '\|^/run/dante-virgil-generated/chrony\.conf r,$|d' "$APPARMOR_LOCAL"
    if [[ -d /sys/kernel/security/apparmor ]] && command -v apparmor_parser >/dev/null; then
        apparmor_parser -r "$APPARMOR_PROFILE"
    fi
fi

echo "Giving timekeeping back to Debian's chrony"
systemctl unmask chrony.service >/dev/null 2>&1 || true
systemctl enable --now chrony.service

if $purge; then
    echo "Removing the config and the $SERVICE_USER user"
    rm -rf "$CONFIG_DIR"
    userdel "$SERVICE_USER" 2>/dev/null || true
else
    echo "The config is still in $CONFIG_DIR."
fi
