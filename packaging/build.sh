#!/bin/bash
# Build Virgil's Debian package, and the offline bundle, for this machine's architecture:
#
#   packaging/build.sh VERSION [OUTPUT_FOLDER]          for example: packaging/build.sh 1.0.0 dist
#
# Run it on Debian 13, as root (it asks apt which Debian packages the offline bundle needs), on the
# architecture being built (amd64 or arm64): pip downloads the wheels for this Python and this CPU.
# The release workflow (.github/workflows/release.yml) runs it on GitHub's amd64 and arm64 machines.
#
# BARE_STATUS can name a copy of /var/lib/dpkg/status from a bare Debian 13 (the workflow saves one before
# installing the build's own tools), so the offline bundle includes everything a bare system lacks, even what
# this machine has installed for building (python3, for one).
#
# For testing the package itself away from Debian 13: PYTHON names a Python 3.13 to download the wheels with, and
# NO_BUNDLE=1 skips the offline bundle (apt elsewhere would fetch the wrong distribution's packages).
#
# Makes, in the output folder:
#   dante-virgil_VERSION_ARCH.deb              needs Debian's own packages (chrony, linuxptp...) from the network
#   dante-virgil-offline_VERSION_ARCH.tar      the .deb, those Debian packages too, and install.sh to install offline

set -euo pipefail
# A cache is no use to a one-off build (and in GitHub's container, its folder belongs to another user).
export PIP_NO_CACHE_DIR=1

die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }

[[ $# -ge 1 ]] || die "usage: packaging/build.sh VERSION [OUTPUT_FOLDER]"
VERSION=$1
OUT=$(realpath -m "${2:-dist}")
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ARCH=$(dpkg --print-architecture)
PREFIX=/opt/dante-virgil

[[ $EUID -eq 0 ]] || die "run this as root: it asks apt which Debian packages the offline bundle needs"
[[ $ARCH == amd64 || $ARCH == arm64 ]] || die "Virgil is built for amd64 and arm64, and this is $ARCH"
PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))' \
    || die "this needs Debian 13's Python 3.13, so the wheels match the Clock Boxes', and this is $("$PYTHON" --version)"

# The tag says what's being released; pyproject.toml says what Virgil reports. They must agree.
declared=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$REPO/pyproject.toml" | head -1)
[[ $declared == "$VERSION" ]] \
    || die "pyproject.toml says version $declared, but this is being built as $VERSION: set version = \"$VERSION\" there (and run uv lock)"
python3 "$REPO/tools/check_requirements.py" \
    || die "requirements.txt doesn't match pyproject.toml and uv.lock (see above): run uv lock, then uv export"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
ROOT=$WORK/root
WHEELS=$ROOT$PREFIX/wheels
mkdir -p "$OUT" "$WHEELS"

echo "==> Downloading Virgil's dependencies, checking each against requirements.txt's hashes"
"$PYTHON" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" download --quiet --only-binary=:all: --no-deps --require-hashes \
    -r "$REPO/requirements.txt" -d "$WHEELS"

echo "==> Building Virgil's own wheel, with the pinned build tools"
"$WORK/venv/bin/pip" install --quiet --no-deps --require-hashes -r "$REPO/build-requirements.txt"
"$WORK/venv/bin/pip" wheel --quiet --no-deps --no-build-isolation -w "$WHEELS" "$REPO"
ls "$WHEELS"/dante_virgil-"$VERSION"-*.whl >/dev/null 2>&1 || die "Virgil's wheel wasn't built as version $VERSION"

echo "==> Pinning them for installing offline"
python3 "$REPO/packaging/pin_wheels.py" "$WHEELS" --except dante-virgil > "$ROOT$PREFIX/requirements.lock"
python3 "$REPO/packaging/pin_wheels.py" "$WHEELS" --only dante-virgil > "$ROOT$PREFIX/virgil.lock"

echo "==> Laying out the package"
install -d "$ROOT/usr/lib/systemd/system" "$ROOT/usr/lib/tmpfiles.d" "$ROOT/usr/share/polkit-1/rules.d" \
    "$ROOT/usr/bin" "$ROOT/usr/share/doc/dante-virgil" "$ROOT/DEBIAN"
install -m 644 "$REPO"/system/systemd/*.service "$ROOT/usr/lib/systemd/system/"
install -m 644 "$REPO/system/tmpfiles.d/dante-virgil.conf" "$ROOT/usr/lib/tmpfiles.d/"
install -m 644 "$REPO/system/polkit/50-dante-virgil.rules" "$ROOT/usr/share/polkit-1/rules.d/"
ln -s "$PREFIX/venv/bin/virgilctl" "$ROOT/usr/bin/virgilctl"
ln -s "$PREFIX/venv/bin/netaudio" "$ROOT/usr/bin/netaudio"
install -m 644 "$REPO/LICENSE" "$ROOT/usr/share/doc/dante-virgil/copyright"
install -m 644 "$REPO/README.md" "$ROOT/usr/share/doc/dante-virgil/README.md"
install -d "$ROOT/usr/share/lintian/overrides"
install -m 644 "$REPO/packaging/debian/lintian-overrides" "$ROOT/usr/share/lintian/overrides/dante-virgil"
MAINTAINER=$(sed -n 's/^Maintainer: //p' "$REPO/packaging/debian/control")
printf 'dante-virgil (%s) unstable; urgency=medium\n\n  * Virgil %s: see https://github.com/BRCook/dante-virgil/releases\n\n -- %s  %s\n' \
    "$VERSION" "$VERSION" "$MAINTAINER" "$(date -R)" | gzip -9n > "$ROOT/usr/share/doc/dante-virgil/changelog.gz"
for script in postinst prerm postrm; do
    install -m 755 "$REPO/packaging/debian/$script" "$ROOT/DEBIAN/$script"
done
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@SIZE@/$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)/" \
    "$REPO/packaging/debian/control" > "$ROOT/DEBIAN/control"
# dpkg-deb refuses a control file without a final newline, which an editor can easily drop.
[[ -z $(tail -c 1 "$ROOT/DEBIAN/control") ]] || echo >> "$ROOT/DEBIAN/control"

DEB=$OUT/dante-virgil_${VERSION}_${ARCH}.deb
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
echo "    $DEB"
[[ -z ${NO_BUNDLE:-} ]] || exit 0

echo "==> Gathering the Debian packages a bare Debian 13 lacks, for the offline bundle"
BUNDLE=dante-virgil-offline_${VERSION}_${ARCH}
mkdir -p "$WORK/$BUNDLE/debs" "$WORK/apt/partial"
apt-get update -qq
# apt works out what the package needs beyond what a bare Debian 13 has (see BARE_STATUS), and fetches it.
apt-get install -qq --download-only --no-install-recommends -o Dir::Cache::archives="$WORK/apt" \
    -o Dir::State::status="${BARE_STATUS:-/var/lib/dpkg/status}" "$DEB" pci.ids >/dev/null
cp "$WORK"/apt/*.deb "$WORK/$BUNDLE/debs/"
cp "$DEB" "$WORK/$BUNDLE/"
install -m 755 "$REPO/packaging/install-offline.sh" "$WORK/$BUNDLE/install.sh"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@ARCH@/$ARCH/g" "$REPO/packaging/README-offline.txt" > "$WORK/$BUNDLE/README.txt"
tar -C "$WORK" -cf "$OUT/$BUNDLE.tar" "$BUNDLE"
echo "    $OUT/$BUNDLE.tar ($(ls "$WORK/$BUNDLE/debs" | wc -l) Debian packages)"
