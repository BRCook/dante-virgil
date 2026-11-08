#!/bin/bash
# Install or update Virgil on this machine. Run it from a copy of the repo:
#
#   sudo ./install.sh
#
# Running it again from a newer copy updates Virgil, and keeps this Virgil's config.

# Restart under bash if started with sh, which can't run this script.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

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

# Each step reports how long the one before it took.
STEP_STARTED=$SECONDS
say() {
    if [[ -n "${STEP_NAME:-}" ]]; then
        printf '    (%ds)\n' $((SECONDS - STEP_STARTED))
    fi
    STEP_NAME="$*"
    STEP_STARTED=$SECONDS
    printf '\n==> %s\n' "$*"
}
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

. /etc/os-release
if [[ "${ID:-}" != debian || "${VERSION_ID:-}" != 13 ]]; then
    printf 'Warning: Virgil is built for Debian 13, and this is %s.\n' "${PRETTY_NAME:-something else}"
fi

PACKAGES=(chrony linuxptp ethtool iproute2 openssl python3-venv polkitd)
missing=()
for package in "${PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed"; then
        missing+=("$package")
    fi
done
if (( ${#missing[@]} )); then
    say "Installing Debian packages: ${missing[*]}"
    apt-get update
    apt-get install -y --no-install-recommends "${missing[@]}"
else
    say "The Debian packages Virgil needs are already installed"
fi

say "Creating the $SERVICE_USER user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# phc2sys has no query interface; virgild reads its reports from the journal.
usermod -a -G systemd-journal "$SERVICE_USER"

# The virtual environment is only rebuilt when requirements.txt changes (or
# Python does); otherwise only Virgil itself is reinstalled, which is quick.
REQUIREMENTS_STAMP="$VENV/.requirements.sha256"
wanted_stamp="$(sha256sum "$REPO/requirements.txt" | cut -d' ' -f1) $(python3 --version)"
if [[ -x "$VENV/bin/python" && -f "$REQUIREMENTS_STAMP" && "$(cat "$REQUIREMENTS_STAMP")" == "$wanted_stamp" ]]; then
    say "Updating Virgil in $VENV"
else
    say "Installing Virgil and its dependencies into $VENV (this takes a few minutes)"
    python3 -m venv --clear "$VENV"
    "$VENV/bin/pip" install --quiet --no-deps --require-hashes -r "$REPO/requirements.txt"
    printf '%s\n' "$wanted_stamp" > "$REQUIREMENTS_STAMP"
fi
"$VENV/bin/pip" install --quiet --no-deps --force-reinstall "$REPO"
ln -sf "$VENV/bin/virgilctl" /usr/local/bin/virgilctl
# netaudio too, unless someone has a netaudio of their own there.
if [[ -e /usr/local/bin/netaudio && "$(readlink /usr/local/bin/netaudio)" != "$VENV/bin/netaudio" ]]; then
    echo "Leaving /usr/local/bin/netaudio alone: it isn't Virgil's"
elif [[ -x "$VENV/bin/netaudio" ]]; then
    ln -sf "$VENV/bin/netaudio" /usr/local/bin/netaudio
fi

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
STEP_NAME=""
echo "Virgil is installed, and this Virgil's config is at $CONFIG_DIR/virgil.toml."
if ! grep -q '^ptp_interface' "$CONFIG_DIR/virgil.toml"; then
    cat << MESSAGE

Next, choose the interface that faces the console:

  virgilctl interfaces
  sudo virgilctl config set ptp_interface <name>
MESSAGE
fi
