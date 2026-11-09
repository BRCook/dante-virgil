from pathlib import Path

from virgil.nicnames import nic_name, parse, shorten

SAMPLE = """# A sample of pci.ids
10ec  Realtek Semiconductor Co., Ltd.
\t8125  RTL8125 2.5GbE Controller
\t8168  RTL8111/8168/8211/8411 PCI Express Gigabit Ethernet Controller
\t\t1043 8677  Some board
8086  Intel Corporation
\t1533  I210 Gigabit Network Connection
\t125c  Ethernet Controller I226-V
C 02  Network controller
\t00  Ethernet controller
"""


def test_parse():
    db = parse(SAMPLE)
    assert db["8086"][0] == "Intel Corporation"
    assert db["8086"][1]["125c"] == "Ethernet Controller I226-V"
    assert "02" not in db  # device classes aren't vendors


def test_shorten():
    assert shorten("Intel Corporation", "Ethernet Controller I226-V") == "Intel I226-V"
    assert shorten("Intel Corporation", "I210 Gigabit Network Connection") == "Intel I210"
    assert shorten("Realtek Semiconductor Co., Ltd.",
                   "RTL8111/8168/8211/8411 PCI Express Gigabit Ethernet Controller") == "Realtek RTL8111/8168/8211/8411"
    assert shorten("Realtek Semiconductor Co., Ltd.", "RTL8125 2.5GbE Controller") == "Realtek RTL8125 2.5GbE"


def _sysfs(tmp_path: Path, name: str, vendor: str, device: str) -> Path:
    folder = tmp_path / name / "device"
    folder.mkdir(parents=True)
    (folder / "vendor").write_text(f"0x{vendor}\n")
    (folder / "device").write_text(f"0x{device}\n")
    return tmp_path


def test_nic_name(tmp_path):
    db = parse(SAMPLE)
    sysfs = _sysfs(tmp_path, "enp1s0", "8086", "125c")
    _sysfs(tmp_path, "enp191s0", "10ec", "8126")  # newer than this database
    (tmp_path / "br0").mkdir()
    assert nic_name("enp1s0", sysfs, db) == "Intel I226-V"
    assert nic_name("enp191s0", sysfs, db) is None
    assert nic_name("br0", sysfs, db) is None
