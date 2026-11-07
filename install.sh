#!/bin/bash
# Install or update Virgil on this machine. Run it from a copy of the repo:
#
#   sudo ./install.sh
#
# Running it again from a newer copy updates Virgil, and keeps this Virgil's config.

set -euo pipefail

PREFIX=/opt/dante-virgil
VENV="$PREFIX/venv"
SERVICE_USER=dante-virgil
CONFIG_DIR=/etc/dante-virgil
UNIT_DIR=/usr/local/lib/systemd/system
TMPFILES_DIR=/usr/local/lib/tmpfiles.d
POLKIT_DIR=/etc/polkit-1/rules.d
APPARMOR_PROFILE=/etc/apparmor.d/usr.sbin.chronyd
APPARMOR_LOCAL=/etc/apparmor.d/local/usr.sbin.chronyd
APPARMOR_RULE='/run/dante-virgil-generated/chrony.conf r,'
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

. /etc/os-release
if [[ "${ID:-}" != debian || "${VERSION_ID:-}" != 13 ]]; then
    printf 'Warning: Virgil is built for Debian 13, and this is %s.\n' "${PRETTY_NAME:-something else}"
fi

say "Installing Debian packages"
apt-get update
apt-get install -y --no-install-recommends chrony linuxptp ethtool iproute2 openssl python3-venv polkitd

say "Creating the $SERVICE_USER user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Installing Virgil into $VENV"
python3 -m venv --clear "$VENV"
"$VENV/bin/pip" install --quiet --no-deps --require-hashes -r "$REPO/requirements.txt"
"$VENV/bin/pip" install --quiet --no-deps "$REPO"
if [[ -n "${NETAUDIO_WHEEL:-}" ]]; then
    # For development, until netaudio's wheels are pinned in requirements.txt:
    #   sudo NETAUDIO_WHEEL=/path/to/netaudio-....whl ./install.sh
    echo "Installing netaudio from $NETAUDIO_WHEEL (its dependencies come from PyPI, unpinned)"
    "$VENV/bin/pip" install --quiet "$NETAUDIO_WHEEL"
fi
ln -sf "$VENV/bin/virgilctl" /usr/local/bin/virgilctl

say "Installing the systemd units, tmpfiles entry and polkit rule"
install -d "$UNIT_DIR" "$TMPFILES_DIR" "$POLKIT_DIR"
install -m 644 "$REPO"/system/systemd/*.service "$UNIT_DIR/"
install -m 644 "$REPO/system/tmpfiles.d/dante-virgil.conf" "$TMPFILES_DIR/"
install -m 644 "$REPO/system/polkit/50-dante-virgil.rules" "$POLKIT_DIR/"
systemd-tmpfiles --create "$TMPFILES_DIR/dante-virgil.conf"
systemctl daemon-reload

if [[ -e "$APPARMOR_PROFILE" ]]; then
    say "Letting chronyd read Virgil's config through AppArmor"
    # Debian's profile includes this local file, which is meant for additions like this one.
    if ! grep -qxF "$APPARMOR_RULE" "$APPARMOR_LOCAL" 2>/dev/null; then
        install -d "$(dirname "$APPARMOR_LOCAL")"
        printf '# Added by Virgil\n%s\n' "$APPARMOR_RULE" >> "$APPARMOR_LOCAL"
    fi
    if [[ -d /sys/kernel/security/apparmor ]] && command -v apparmor_parser >/dev/null; then
        apparmor_parser -r "$APPARMOR_PROFILE"
    fi
fi

say "Handing timekeeping over from Debian's chrony to Virgil"
systemctl disable --now chrony.service >/dev/null 2>&1 || true
systemctl mask chrony.service >/dev/null 2>&1

say "Setting up $CONFIG_DIR"
# 751: anyone can reach the public certificate inside (for virgilctl status),
# but only dante-virgil can list the directory. The config and key stay private.
install -d -m 751 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR"
if [[ -e "$CONFIG_DIR/virgil.toml" ]]; then
    echo "Keeping the existing config."
else
    "$VENV/bin/virgilctl" config init
fi
"$VENV/bin/virgilctl" tls init
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/tls"
chmod 755 "$CONFIG_DIR/tls"
chmod 644 "$CONFIG_DIR/tls/cert.pem"
chmod 600 "$CONFIG_DIR/tls/key.pem"

say "Starting virgild"
systemctl enable dante-virgil.service >/dev/null 2>&1
systemctl restart dante-virgil.service

say "Done"
cat << MESSAGE
Virgil is installed, and this Virgil's config is at $CONFIG_DIR/virgil.toml.

Next, choose the interface that faces the console:

  virgilctl interfaces
  sudo virgilctl config set ptp_interface <name>
MESSAGE
