"""Network interfaces, and what each one can do for PTP.

Timestamping capabilities come straight from the kernel with the
ETHTOOL_GET_TS_INFO ioctl, the same call `ethtool -T` makes, so there's no
ethtool output to parse and no root needed.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from virgil.nic_profiles import NicProfile, profile_for

SYSFS_NET = Path("/sys/class/net")

SIOCETHTOOL = 0x8946
ETHTOOL_GET_TS_INFO = 0x41
IFNAMSIZ = 16

# SOF_TIMESTAMPING_* flags, from linux/net_tstamp.h.
TX_HARDWARE = 1 << 0
TX_SOFTWARE = 1 << 1
RX_HARDWARE = 1 << 2
RX_SOFTWARE = 1 << 3
SOFTWARE = 1 << 4
RAW_HARDWARE = 1 << 6

# HWTSTAMP_FILTER_* values, as bits in ethtool_ts_info.rx_filters.
FILTER_NONE = 1 << 0
FILTER_ALL = 1 << 1


class _TsInfo(ctypes.Structure):
    """struct ethtool_ts_info, from linux/ethtool.h."""

    _fields_ = [
        ("cmd", ctypes.c_uint32),
        ("so_timestamping", ctypes.c_uint32),
        ("phc_index", ctypes.c_int32),
        ("tx_types", ctypes.c_uint32),
        ("tx_reserved", ctypes.c_uint32 * 3),
        ("rx_filters", ctypes.c_uint32),
        ("rx_reserved", ctypes.c_uint32 * 3),
    ]


class _IfReq(ctypes.Structure):
    """struct ifreq, with the ifr_data member of its union."""

    _fields_ = [
        ("ifr_name", ctypes.c_char * IFNAMSIZ),
        ("ifr_data", ctypes.c_void_p),
        ("_pad", ctypes.c_char * 16),
    ]


class Timestamping(Enum):
    HARDWARE = "hardware"
    SOFTWARE = "software"
    NONE = "none"


@dataclass(frozen=True)
class TimestampingInfo:
    so_timestamping: int
    phc_index: int
    tx_types: int
    rx_filters: int

    @property
    def mode(self) -> Timestamping:
        hardware = TX_HARDWARE | RX_HARDWARE | RAW_HARDWARE
        software = TX_SOFTWARE | RX_SOFTWARE | SOFTWARE
        if self.so_timestamping & hardware == hardware and self.phc_index >= 0:
            return Timestamping.HARDWARE
        if self.so_timestamping & software == software:
            return Timestamping.SOFTWARE
        return Timestamping.NONE

    @property
    def phc_device(self) -> str | None:
        if self.phc_index < 0:
            return None
        return f"/dev/ptp{self.phc_index}"

    @property
    def rx_filter_all_only(self) -> bool:
        """True if the only receive filters are 'none' and 'all', as on the i226."""
        return self.rx_filters == FILTER_NONE | FILTER_ALL


def read_timestamping(name: str) -> TimestampingInfo | None:
    """Ask the kernel about an interface's timestamping, or None if it can't say."""
    info = _TsInfo(cmd=ETHTOOL_GET_TS_INFO)
    request = _IfReq(ifr_name=name.encode()[: IFNAMSIZ - 1])
    request.ifr_data = ctypes.addressof(info)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            fcntl.ioctl(sock.fileno(), SIOCETHTOOL, request)
    except OSError:
        return None
    return TimestampingInfo(
        so_timestamping=info.so_timestamping,
        phc_index=info.phc_index,
        tx_types=info.tx_types,
        rx_filters=info.rx_filters,
    )


@dataclass(frozen=True)
class Interface:
    name: str
    mac: str | None
    driver: str | None
    physical: bool
    link_up: bool
    timestamping: TimestampingInfo | None

    @property
    def mode(self) -> Timestamping:
        if self.timestamping is None:
            return Timestamping.NONE
        return self.timestamping.mode

    @property
    def profile(self) -> NicProfile | None:
        return profile_for(self.driver)

    @property
    def ptp_capable(self) -> bool:
        """Whether this interface can be offered as a PTP interface at all."""
        return self.physical and self.mode is not Timestamping.NONE


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _interface(
    entry: Path,
    timestamping: Callable[[str], TimestampingInfo | None],
) -> Interface:
    device = entry / "device"
    driver_link = device / "driver"
    driver = os.path.basename(os.readlink(driver_link)) if driver_link.is_symlink() else None
    return Interface(
        name=entry.name,
        mac=_read(entry / "address"),
        driver=driver,
        physical=device.exists(),
        link_up=_read(entry / "operstate") == "up",
        timestamping=timestamping(entry.name),
    )


def list_interfaces(
    sysfs: Path = SYSFS_NET,
    timestamping: Callable[[str], TimestampingInfo | None] = read_timestamping,
) -> list[Interface]:
    """List every network interface except loopback, sorted by name."""
    return [_interface(entry, timestamping) for entry in sorted(sysfs.iterdir()) if entry.name != "lo"]


def get_interface(
    name: str,
    sysfs: Path = SYSFS_NET,
    timestamping: Callable[[str], TimestampingInfo | None] = read_timestamping,
) -> Interface | None:
    """Look up one interface by name, or None if it doesn't exist."""
    if "/" in name or name in ("", ".", ".."):
        return None
    entry = sysfs / name
    if not entry.exists():
        return None
    return _interface(entry, timestamping)
