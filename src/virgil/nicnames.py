"""Real names for NICs, from the PCI ID database.

The kernel gives each PCI network interface's vendor and device IDs in
/sys/class/net/<interface>/device/, and Debian's pci.ids package turns them
into names. Those are long ("Intel Corporation Ethernet Controller I226-V"),
so Virgil shortens them ("Intel I226-V"). A NIC the database doesn't know yet
gets no name here, and Virgil falls back to its NIC profiles' names.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

DATABASES = (Path("/usr/share/misc/pci.ids"), Path("/usr/share/hwdata/pci.ids"))
SYSFS = Path("/sys/class/net")
_VENDOR = re.compile(r"^([0-9a-f]{4})  (.+)$")
_DEVICE = re.compile(r"^\t([0-9a-f]{4})  (.+)$")
# Words that say it's a network controller, which the dropdown already makes plain.
_NOISE = re.compile(
    r"\b(PCI Express|PCIe|Gigabit|Ethernet|Network|Connection|Controller|Adapter|Family)\b", re.IGNORECASE)


def parse(text: str) -> dict[str, tuple[str, dict[str, str]]]:
    """pci.ids, as vendor ID to (vendor name, device ID to device name)."""
    vendors: dict[str, tuple[str, dict[str, str]]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("C "):  # device classes start here; vendors are done
            break
        match = _VENDOR.match(line)
        if match:
            current = {}
            vendors[match.group(1)] = (match.group(2), current)
            continue
        match = _DEVICE.match(line)
        if match and current is not None:
            current[match.group(1)] = match.group(2)
    return vendors


@functools.lru_cache(maxsize=1)
def database() -> dict[str, tuple[str, dict[str, str]]]:
    for path in DATABASES:
        try:
            return parse(path.read_text(errors="replace"))
        except OSError:
            continue
    return {}


def shorten(vendor: str, device: str) -> str:
    """'Intel Corporation' and 'Ethernet Controller I226-V' as 'Intel I226-V'."""
    brand = re.split(r"[\s,]+", vendor.strip())[0]
    model = re.sub(r"\s+", " ", _NOISE.sub(" ", device)).strip(" ,-")
    return f"{brand} {model or device}".strip()


def pci_ids(interface: str, sysfs: Path = SYSFS) -> tuple[str, str] | None:
    try:
        device = sysfs / interface / "device"
        vendor_id = (device / "vendor").read_text().strip().lower().removeprefix("0x")
        device_id = (device / "device").read_text().strip().lower().removeprefix("0x")
    except OSError:
        return None
    return vendor_id, device_id


def nic_name(interface: str, sysfs: Path = SYSFS, db: dict | None = None) -> str | None:
    """The interface's NIC, such as 'Intel I226-V', or None if it isn't known."""
    ids = pci_ids(interface, sysfs)
    if ids is None:
        return None
    vendors = database() if db is None else db
    vendor = vendors.get(ids[0])
    if vendor is None or ids[1] not in vendor[1]:
        return None
    return shorten(vendor[0], vendor[1][ids[1]])
