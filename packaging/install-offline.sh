#!/bin/bash
# Install Virgil without the network, from this bundle: its package, and the Debian packages it needs that this
# machine doesn't already have. Packages already installed are left alone, so nothing is ever downgraded.
#
#   sudo ./install.sh

if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")"
[[ $EUID -eq 0 ]] || die "run this with sudo"
virgil=(dante-virgil_*.deb)
[[ -e ${virgil[0]} ]] || die "this bundle has no dante-virgil package in it"
wanted_arch=$(dpkg-deb -f "${virgil[0]}" Architecture)
[[ $(dpkg --print-architecture) == "$wanted_arch" ]] \
    || die "this bundle is for $wanted_arch, and this machine is $(dpkg --print-architecture)"

needed=()
for deb in debs/*.deb; do
    package=$(dpkg-deb -f "$deb" Package)
    arch=$(dpkg-deb -f "$deb" Architecture)
    query=$package
    [[ $arch == all ]] || query="$package:$arch"
    if dpkg-query -W -f='${Status}' "$query" 2>/dev/null | grep -q "install ok installed"; then
        continue
    fi
    needed+=("./$deb")
done
if [[ ${#needed[@]} -eq 1 ]]; then noun=package; else noun=packages; fi
echo "Installing Virgil, and ${#needed[@]} Debian $noun this machine doesn't have yet"
# Every package needed is given to apt as a file, so it has nothing to fetch. (apt's --no-download would make that
# explicit, but it refuses packages given as files altogether.)
apt-get install -y --no-install-recommends "${needed[@]}" "./${virgil[0]}"
