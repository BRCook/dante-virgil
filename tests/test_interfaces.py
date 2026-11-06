import os
from pathlib import Path

from virgil import interfaces as ifc
from virgil.interfaces import Timestamping, TimestampingInfo, list_interfaces

HARDWARE = (
    ifc.TX_HARDWARE | ifc.RX_HARDWARE | ifc.RAW_HARDWARE
    | ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE
)
SOFTWARE_ONLY = ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE

I226 = TimestampingInfo(HARDWARE, phc_index=0, tx_types=0b11, rx_filters=ifc.FILTER_NONE | ifc.FILTER_ALL)
RTL8111 = TimestampingInfo(SOFTWARE_ONLY, phc_index=-1, tx_types=0, rx_filters=0)


def test_hardware_timestamping():
    assert I226.mode is Timestamping.HARDWARE
    assert I226.phc_device == "/dev/ptp0"
    assert I226.rx_filter_all_only


def test_software_timestamping_only():
    assert RTL8111.mode is Timestamping.SOFTWARE
    assert RTL8111.phc_device is None


def test_hardware_flags_without_a_phc_are_not_hardware():
    info = TimestampingInfo(HARDWARE, phc_index=-1, tx_types=0, rx_filters=0)
    assert info.mode is Timestamping.SOFTWARE


def test_no_timestamping():
    assert TimestampingInfo(0, -1, 0, 0).mode is Timestamping.NONE


def test_rx_filters_beyond_all():
    info = TimestampingInfo(HARDWARE, 1, 0b11, ifc.FILTER_NONE | ifc.FILTER_ALL | (1 << 12))
    assert not info.rx_filter_all_only


def _fake_interface(root: Path, name: str, driver: str | None, operstate: str = "up") -> None:
    entry = root / name
    entry.mkdir()
    (entry / "address").write_text("aa:bb:cc:dd:ee:ff\n")
    (entry / "operstate").write_text(f"{operstate}\n")
    if driver is not None:
        drivers = root.parent / "drivers"
        (drivers / driver).mkdir(parents=True, exist_ok=True)
        (entry / "device").mkdir()
        os.symlink(drivers / driver, entry / "device" / "driver")


def test_list_interfaces(tmp_path):
    root = tmp_path / "net"
    root.mkdir()
    _fake_interface(root, "lo", None)
    _fake_interface(root, "enp1s0", "igc")
    _fake_interface(root, "enp5s0", "r8169", operstate="down")
    _fake_interface(root, "br0", None)

    infos = {"enp1s0": I226, "enp5s0": RTL8111}
    found = {i.name: i for i in list_interfaces(root, lambda name: infos.get(name))}

    assert sorted(found) == ["br0", "enp1s0", "enp5s0"]

    i226 = found["enp1s0"]
    assert i226.driver == "igc" and i226.physical and i226.link_up
    assert i226.mode is Timestamping.HARDWARE and i226.ptp_capable
    assert i226.profile is not None and i226.profile.description == "Intel i225/i226"

    rtl = found["enp5s0"]
    assert rtl.mode is Timestamping.SOFTWARE and not rtl.link_up and rtl.ptp_capable

    bridge = found["br0"]
    assert not bridge.physical and bridge.driver is None and not bridge.ptp_capable
