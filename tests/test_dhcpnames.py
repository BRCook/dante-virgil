from virgil.dhcpnames import MARKER, apply, host_label, wanted_names

BOX = """# This file describes the network interfaces available on your system
# and how to activate them. For more information, see interfaces(5).

source /etc/network/interfaces.d/*

# The loopback network interface
auto lo
iface lo inet loopback

# The primary network interface
allow-hotplug enp5s0
iface enp5s0 inet dhcp

allow-hotplug enp1s0
iface enp1s0 inet dhcp
"""


def test_names():
    assert host_label("debian-server-1", "ptp") == "debian-server-1-ptp"
    assert host_label("Debian_Server.example.com", "mgmt") == "debian-server-mgmt"
    assert len(host_label("x" * 80, "mgmt")) == 63
    assert wanted_names("box", "enp1s0", "enp5s0") == {"enp1s0": "box-ptp", "enp5s0": "box-mgmt"}
    assert wanted_names("box", "enp1s0", "enp1s0") == {"enp1s0": "box-mgmt"}


def test_the_real_file():
    text, notes = apply(BOX, wanted_names("debian-server-1", "enp1s0", "enp5s0"))
    assert f"iface enp5s0 inet dhcp\n    {MARKER}\n    hostname debian-server-1-mgmt\n\nallow-hotplug enp1s0" in text
    assert text.endswith(f"iface enp1s0 inet dhcp\n    {MARKER}\n    hostname debian-server-1-ptp\n")
    assert "iface lo inet loopback\n\n" in text  # untouched
    assert sorted(notes) == ["enp1s0 will ask for the name debian-server-1-ptp",
                             "enp5s0 will ask for the name debian-server-1-mgmt"]


def test_applying_again_changes_nothing():
    names = wanted_names("debian-server-1", "enp1s0", "enp5s0")
    once, _ = apply(BOX, names)
    twice, _ = apply(once, names)
    assert twice == once


def test_changing_interfaces_moves_the_names():
    once, _ = apply(BOX, wanted_names("box", "enp1s0", "enp5s0"))
    swapped, _ = apply(once, wanted_names("box", "enp5s0", "enp1s0"))
    assert "iface enp5s0 inet dhcp\n    # Set by Virgil\n    hostname box-ptp" in swapped
    assert "iface enp1s0 inet dhcp\n    # Set by Virgil\n    hostname box-mgmt" in swapped
    assert swapped.count(MARKER) == 2


def test_removing():
    once, _ = apply(BOX, wanted_names("box", "enp1s0", "enp5s0"))
    removed, _ = apply(once, {})
    assert removed == BOX


def test_a_hostname_someone_wrote_is_left_alone():
    mine = BOX.replace("iface enp1s0 inet dhcp\n", "iface enp1s0 inet dhcp\n    hostname my-own-name\n")
    text, notes = apply(mine, wanted_names("box", "enp1s0", "enp5s0"))
    assert "hostname my-own-name" in text and "hostname box-ptp" not in text
    assert "enp1s0 already has a hostname of its own; leaving it" in notes


def test_static_and_missing_interfaces():
    static = BOX.replace("iface enp1s0 inet dhcp", "iface enp1s0 inet static\n    address 10.0.0.2/24")
    text, notes = apply(static, wanted_names("box", "enp1s0", "enp9s0"))
    assert "hostname box-ptp" not in text
    assert "enp1s0 doesn't use DHCP, so it has no lease to name" in notes
    assert "enp9s0 isn't configured in this file, so its lease can't be named here" in notes


DHCPCD = """# A sample configuration for dhcpcd.
hostname
duid
persistent
option rapid_commit
slaac private
"""


def test_nogateway_for_the_ptp_interface():
    from virgil.dhcpnames import BLOCK_END, BLOCK_START, apply_dhcpcd

    text = apply_dhcpcd(DHCPCD, "enp1s0", "enp5s0")
    assert text.startswith(DHCPCD)
    assert text.endswith(f"{BLOCK_START}\n# The PTP interface faces a console, so it mustn't become the default "
                         f"route.\ninterface enp1s0\nnogateway\n{BLOCK_END}\n")
    assert apply_dhcpcd(text, "enp1s0", "enp5s0") == text  # running again changes nothing
    moved = apply_dhcpcd(text, "enp2s0", "enp5s0")
    assert "interface enp2s0" in moved and "interface enp1s0" not in moved
    assert apply_dhcpcd(text, "enp1s0", "enp1s0") == DHCPCD  # one interface for both: it needs its gateway
    assert apply_dhcpcd("", "enp1s0", "enp5s0").startswith(BLOCK_START)
