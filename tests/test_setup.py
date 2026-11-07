import pytest

from virgil import interfaces as ifc
from virgil.interfaces import Interface, TimestampingInfo
from virgil.setup import SetupError, check_mgmt_interface, check_ptp_interface, describe_interfaces

HARDWARE = ifc.TX_HARDWARE | ifc.RX_HARDWARE | ifc.RAW_HARDWARE | ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE
SOFTWARE = ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE
INTERFACES = {
    "enp1s0": Interface("enp1s0", "aa", "igc", True, True, TimestampingInfo(HARDWARE, 0, 3, 3)),
    "enp191s0": Interface("enp191s0", "bb", "r8126", True, True, TimestampingInfo(HARDWARE, 0, 3, 1 << 12)),
    "enp2s0": Interface("enp2s0", "cc", "e1000e", True, False, TimestampingInfo(HARDWARE, 1, 3, 3)),
    "enp5s0": Interface("enp5s0", "dd", "r8169", True, True, TimestampingInfo(SOFTWARE, -1, 0, 0)),
    "br0": Interface("br0", "ee", None, False, True, TimestampingInfo(SOFTWARE, -1, 0, 0)),
}
lookup = INTERFACES.get


def test_a_tested_nic():
    choice = check_ptp_interface("enp1s0", lookup=lookup)
    assert (choice.nic_profile, choice.needs) == ("igc", None)


def test_a_profile_needs_agreeing_to():
    asked = check_ptp_interface("enp191s0", lookup=lookup)
    assert asked.needs == "profile" and "RTL8126" in asked.message
    assert check_ptp_interface("enp191s0", accept_profile=True, lookup=lookup).nic_profile == "r8126"
    declined = check_ptp_interface("enp191s0", accept_profile=False, lookup=lookup)
    assert declined.nic_profile is None and "probably not work" in declined.note


def test_software_timestamping_needs_agreeing_to():
    asked = check_ptp_interface("enp5s0", lookup=lookup)
    assert asked.needs == "software" and "does not work" in asked.message
    assert check_ptp_interface("enp5s0", accept_software=True, lookup=lookup).needs is None


def test_an_untested_driver():
    choice = check_ptp_interface("enp2s0", lookup=lookup)
    assert choice.needs is None and "hasn't been tested" in choice.note


@pytest.mark.parametrize("name", ["br0", "eth9"])
def test_unusable(name):
    with pytest.raises(SetupError):
        check_ptp_interface(name, lookup=lookup)


def test_management_interface():
    assert check_mgmt_interface("br0", lookup=lookup) == "br0"
    with pytest.raises(SetupError):
        check_mgmt_interface("eth9", lookup=lookup)


def test_describe():
    described = {d["name"]: d for d in describe_interfaces(list(INTERFACES.values()))}
    assert described["enp1s0"]["ptp_capable"] and described["enp1s0"]["profile"]["status"] == "tested"
    assert described["br0"]["timestamping"] == "virtual" and not described["br0"]["ptp_capable"]
    assert described["enp2s0"]["link_up"] is False
