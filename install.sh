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
# netaudio runs as a user of its own, which can't read Virgil's config or keys (see dante-virgil-netaudio.service).
NETAUDIO_USER=dante-virgil-netaudio
NETAUDIO_STATE=/var/lib/dante-virgil-netaudio
CONFIG_DIR=/etc/dante-virgil
UNIT_DIR=/usr/local/lib/systemd/system
TMPFILES_DIR=/usr/local/lib/tmpfiles.d
POLKIT_DIR=/etc/polkit-1/rules.d
APPARMOR_PROFILE=/etc/apparmor.d/usr.sbin.chronyd
APPARMOR_LOCAL=/etc/apparmor.d/local/usr.sbin.chronyd
APPARMOR_RULE='/run/dante-virgil-generated/chrony.conf r,'
# The group's NTP keys (see virgil.ntpauth): chronyd reads them too, for authenticated NTP.
APPARMOR_KEYS_RULE='/run/dante-virgil-generated/chrony.keys r,'
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

# util-linux-extra provides hwclock, which saves the time to the hardware clock after each setup.
PACKAGES=(chrony linuxptp ethtool iproute2 openssl python3-venv polkitd util-linux-extra)
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
# The PCI ID database names NICs properly. It's a nicety, so an offline Virgil carries on without it.
if ! dpkg-query -W -f='${Status}' pci.ids 2>/dev/null | grep -q "install ok installed"; then
    say "Installing the PCI ID database, for NIC names"
    if ! apt-get install -y --no-install-recommends pci.ids; then
        echo "Couldn't install pci.ids (offline?). Virgil will use its own NIC names until it's installed."
    fi
fi

say "Creating the $SERVICE_USER user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# phc2sys has no query interface; virgild reads its reports from the journal.
usermod -a -G systemd-journal "$SERVICE_USER"

say "Creating the $NETAUDIO_USER user"
if ! id -u "$NETAUDIO_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin "$NETAUDIO_USER"
fi

# requirements.txt is what gets installed, so it has to match pyproject.toml and uv.lock. If a step was
# missed (uv lock, or uv export), refuse, rather than quietly install something stale.
if ! python3 "$REPO/tools/check_requirements.py"; then
    echo "Refusing to install: requirements.txt doesn't match pyproject.toml and uv.lock (see above)." >&2
    exit 1
fi

# The virtual environment is only rebuilt when requirements.txt changes (or
# Python does); otherwise only Virgil itself is reinstalled, which is quick.
REQUIREMENTS_STAMP="$VENV/.requirements.sha256"
REBUILT=""
wanted_stamp="$(cat "$REPO/requirements.txt" "$REPO/build-requirements.txt" | sha256sum | cut -d' ' -f1) $(python3 --version)"
if [[ -x "$VENV/bin/python" && -f "$REQUIREMENTS_STAMP" && "$(cat "$REQUIREMENTS_STAMP")" == "$wanted_stamp" ]]; then
    say "Updating Virgil in $VENV"
else
    say "Installing Virgil and its dependencies into $VENV (this takes a few minutes)"
    python3 -m venv --clear "$VENV"
    "$VENV/bin/pip" install --quiet --no-deps --require-hashes -r "$REPO/requirements.txt"
    # The build tools too, pinned and checked like everything else, so building Virgil never fetches one unchecked.
    # They stay in the environment, unused at run time.
    "$VENV/bin/pip" install --quiet --no-deps --require-hashes -r "$REPO/build-requirements.txt"
    printf '%s\n' "$wanted_stamp" > "$REQUIREMENTS_STAMP"
    REBUILT=1
fi
# --no-build-isolation: build with the pinned tools just installed, rather than letting pip fetch its own.
"$VENV/bin/pip" install --quiet --no-deps --no-build-isolation --force-reinstall "$REPO"
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
    for rule in "$APPARMOR_RULE" "$APPARMOR_KEYS_RULE"; do
        if ! grep -qxF "$rule" "$APPARMOR_LOCAL" 2>/dev/null; then
            install -d "$(dirname "$APPARMOR_LOCAL")"
            printf '# Added by Virgil\n%s\n' "$rule" >> "$APPARMOR_LOCAL"
        fi
    done
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
    # As the service user, like the certificate below.
    runuser -u "$SERVICE_USER" -- "$VENV/bin/virgilctl" config init
fi
# From here on, everything in $CONFIG_DIR is done as $SERVICE_USER, never as root. The directory belongs to that
# user, and root working in a directory someone else owns can be misdirected with a symlink: the chmod and chown
# that used to follow here (on tls, tls/cert.pem and tls/key.pem) would have changed whatever those names had been
# pointed at. Made by the service user, the certificate and key are already its own, with the right permissions
# (see virgil.tls.generate), so there's nothing left for root to adjust.
runuser -u "$SERVICE_USER" -- "$VENV/bin/virgilctl" tls init

say "Starting virgild"
systemctl enable dante-virgil.service >/dev/null 2>&1
systemctl restart dante-virgil.service

# Before Virgil 2.0, netaudio ran as $SERVICE_USER, keeping its history and caches in that user's folder. Move them
# to its own, with it stopped, since its history is in SQLite databases it may be writing.
OLD_HOME=/var/lib/dante-virgil
if [[ ! -e "$NETAUDIO_STATE/.local/share/netaudio" ]] && [[ -e "$OLD_HOME/.local/share/netaudio" || \
        -e "$OLD_HOME/.cache/netaudio" || -e "$OLD_HOME/.config/netaudio" ]]; then
    say "Moving netaudio's history to its own folder, $NETAUDIO_STATE"
    systemctl stop dante-virgil-netaudio.service 2>/dev/null || true
    install -d -m 700 -o "$NETAUDIO_USER" -g "$NETAUDIO_USER" "$NETAUDIO_STATE"
    for part in .local/share/netaudio .cache/netaudio .config/netaudio; do
        if [[ -e "$OLD_HOME/$part" ]]; then
            mkdir -p "$NETAUDIO_STATE/$(dirname "$part")"
            mv "$OLD_HOME/$part" "$NETAUDIO_STATE/$part"
        fi
    done
    chown -R "$NETAUDIO_USER:$NETAUDIO_USER" "$NETAUDIO_STATE"
    rmdir "$OLD_HOME/.local/share" "$OLD_HOME/.local" "$OLD_HOME/.cache" "$OLD_HOME/.config" 2>/dev/null || true
fi

# A rebuilt environment may hold a new netaudio, and one still running as $SERVICE_USER is running as the old user:
# either way, restart it. That only pauses monitoring and alerts for a few seconds: netaudio isn't in the timing path.
NETAUDIO_PID=$(systemctl show -p MainPID --value dante-virgil-netaudio.service 2>/dev/null || echo 0)
NETAUDIO_RUNS_AS=$( [[ "$NETAUDIO_PID" != 0 ]] && ps -o user= -p "$NETAUDIO_PID" 2>/dev/null | tr -d ' ' )
if systemctl is-active --quiet dante-virgil-netaudio.service && \
        [[ -n "$REBUILT" || "$NETAUDIO_RUNS_AS" != "$NETAUDIO_USER" ]]; then
    say "Restarting netaudio"
    systemctl restart dante-virgil-netaudio.service
fi

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
