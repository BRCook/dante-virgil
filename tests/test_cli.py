import pytest

from virgil import cli
from virgil import config as config_file
from virgil import interfaces as ifc
from virgil.interfaces import Interface, TimestampingInfo

HARDWARE = ifc.TX_HARDWARE | ifc.RX_HARDWARE | ifc.RAW_HARDWARE | ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE
SOFTWARE = ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE

INTERFACES = {
    "enp1s0": Interface("enp1s0", "aa", "igc", True, True, TimestampingInfo(HARDWARE, 0, 3, 3)),
    "enp191s0": Interface("enp191s0", "bb", "r8126", True, True, TimestampingInfo(HARDWARE, 0, 3, 1 << 12)),
    "enp2s0": Interface("enp2s0", "cc", "e1000e", True, True, TimestampingInfo(HARDWARE, 1, 3, 3)),
    "enp5s0": Interface("enp5s0", "dd", "r8169", True, True, TimestampingInfo(SOFTWARE, -1, 0, 0)),
    "wlan0": Interface("wlan0", "ee", "mt7921e", True, True, TimestampingInfo(0, -1, 0, 0)),
}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_interface", INTERFACES.get)
    path = tmp_path / "virgil.toml"
    assert cli.main(["--config", str(path), "config", "init"]) == 0
    return path


def _set(box, key, value, *extra):
    return cli.main(["--config", str(box), "config", "set", key, value, *extra])


def test_init_refuses_to_overwrite(box):
    assert cli.main(["--config", str(box), "config", "init"]) == 1


def test_tested_nic_needs_no_questions(box):
    assert _set(box, "ptp_interface", "enp1s0") == 0
    config = config_file.load(box)
    assert config.ptp_interface == "enp1s0" and config.nic_profile == "igc"


def test_nic_profile_accepted(box, monkeypatch):
    monkeypatch.setattr(cli, "confirm", lambda question, default, assume_yes: True)
    assert _set(box, "ptp_interface", "enp191s0") == 0
    assert config_file.load(box).nic_profile == "r8126"


def test_nic_profile_declined(box, monkeypatch, capsys):
    monkeypatch.setattr(cli, "confirm", lambda question, default, assume_yes: False)
    assert _set(box, "ptp_interface", "enp191s0") == 0
    assert config_file.load(box).nic_profile is None
    assert "probably not work" in capsys.readouterr().out


def test_untested_driver_gets_defaults(box, capsys):
    assert _set(box, "ptp_interface", "enp2s0") == 0
    assert config_file.load(box).nic_profile is None
    assert "hasn't been tested" in capsys.readouterr().out


def test_software_timestamping_needs_confirmation(box, monkeypatch):
    # The person says no unless --yes was given.
    monkeypatch.setattr(cli, "confirm", lambda question, default, assume_yes: assume_yes)
    assert _set(box, "ptp_interface", "enp5s0") == 1
    assert config_file.load(box).ptp_interface is None
    assert _set(box, "ptp_interface", "enp5s0", "--yes") == 0
    assert config_file.load(box).ptp_interface == "enp5s0"


@pytest.mark.parametrize("name", ["wlan0", "eth9"])
def test_unusable_ptp_interfaces(box, name):
    assert _set(box, "ptp_interface", name) == 1


def test_trim(box):
    assert _set(box, "trim_ppb", "21500") == 0
    assert config_file.load(box).trim_ppb == 21500
    assert _set(box, "trim_ppb", "lots") == 1


def test_role_needs_a_ptp_interface(box, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "current_request", lambda systemd, path: None)
    assert cli.main(["--config", str(box), "--request", str(tmp_path / "r.json"),
                     "role", "leader", "--allow", "10.0.0.0/24"]) == 1


def test_role_follower(box, tmp_path, monkeypatch):
    _set(box, "ptp_interface", "enp1s0")
    applied = []
    monkeypatch.setattr(cli, "current_request", lambda systemd, path: None)
    monkeypatch.setattr(cli, "apply", lambda old, new, systemd, write, log: applied.append(new))
    assert cli.main(["--config", str(box), "--request", str(tmp_path / "r.json"),
                     "role", "follower", "--leader", "192.168.20.21"]) == 0
    (request,) = applied
    assert request.leader == "192.168.20.21" and request.allow == ("192.168.20.21",)
    assert request.use_nic_profile is True


def test_role_rejects_a_bad_leader(box, tmp_path, monkeypatch):
    _set(box, "ptp_interface", "enp1s0")
    monkeypatch.setattr(cli, "current_request", lambda systemd, path: None)
    assert cli.main(["--config", str(box), "--request", str(tmp_path / "r.json"),
                     "role", "follower", "--leader", "box-a"]) == 1


def test_missing_config(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "config", "show"]) == 1
    assert "config init" in capsys.readouterr().err


def test_role_show_without_permission(box, tmp_path, monkeypatch, capsys):
    def denied(systemd, path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cli, "current_request", denied)
    assert cli.main(["--config", str(box), "--request", "/run/dante-virgil/request.json", "role", "show"]) == 1
    assert "Try again with sudo" in capsys.readouterr().err
