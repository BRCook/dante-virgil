import shutil

import pytest

from virgil import cli, tls
from virgil import config as config_file
from virgil import interfaces as ifc
from virgil.group import InviteToken
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
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_interface", INTERFACES.get)
    path = tmp_path / "virgil.toml"
    assert cli.main(["--config", str(path), "config", "init"]) == 0
    return path


def _set(config_path, key, value, *extra):
    return cli.main(["--config", str(config_path), "config", "set", key, value, *extra])


def test_init_refuses_to_overwrite(config_path):
    assert cli.main(["--config", str(config_path), "config", "init"]) == 1


def test_tested_nic_needs_no_questions(config_path):
    assert _set(config_path, "ptp_interface", "enp1s0") == 0
    config = config_file.load(config_path)
    assert config.ptp_interface == "enp1s0" and config.nic_profile == "igc"


def test_nic_profile_accepted(config_path, monkeypatch):
    monkeypatch.setattr(cli, "confirm", lambda question, default, assume_yes: True)
    assert _set(config_path, "ptp_interface", "enp191s0") == 0
    assert config_file.load(config_path).nic_profile == "r8126"


def test_nic_profile_declined(config_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "confirm", lambda question, default, assume_yes: False)
    assert _set(config_path, "ptp_interface", "enp191s0") == 0
    assert config_file.load(config_path).nic_profile is None
    assert "probably not work" in capsys.readouterr().out


def test_untested_driver_gets_defaults(config_path, capsys):
    assert _set(config_path, "ptp_interface", "enp2s0") == 0
    assert config_file.load(config_path).nic_profile is None
    assert "hasn't been tested" in capsys.readouterr().out


def test_software_timestamping_needs_confirmation(config_path, monkeypatch):
    # The person says no unless --yes was given.
    monkeypatch.setattr(cli, "confirm", lambda question, default, assume_yes: assume_yes)
    assert _set(config_path, "ptp_interface", "enp5s0") == 1
    assert config_file.load(config_path).ptp_interface is None
    assert _set(config_path, "ptp_interface", "enp5s0", "--yes") == 0
    assert config_file.load(config_path).ptp_interface == "enp5s0"


@pytest.mark.parametrize("name", ["wlan0", "eth9"])
def test_unusable_ptp_interfaces(config_path, name):
    assert _set(config_path, "ptp_interface", name) == 1


def test_trim(config_path):
    assert _set(config_path, "trim_ppb", "21500") == 0
    assert config_file.load(config_path).trim_ppb == 21500
    assert _set(config_path, "trim_ppb", "lots") == 1


def test_role_needs_a_ptp_interface(config_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "current_request", lambda systemd, path: None)
    assert cli.main(["--config", str(config_path), "--request", str(tmp_path / "r.json"),
                     "role", "leader", "--allow", "10.0.0.0/24"]) == 1


def test_role_follower(config_path, tmp_path, monkeypatch):
    _set(config_path, "ptp_interface", "enp1s0")
    applied = []
    monkeypatch.setattr(cli, "current_request", lambda systemd, path: None)
    monkeypatch.setattr(cli, "apply", lambda old, new, systemd, write, log: applied.append(new))
    assert cli.main(["--config", str(config_path), "--request", str(tmp_path / "r.json"),
                     "role", "follower", "--leader", "192.168.20.21"]) == 0
    (request,) = applied
    assert request.leader == "192.168.20.21" and request.allow == ("192.168.20.21",)
    assert request.use_nic_profile is True


def test_role_rejects_a_bad_leader(config_path, tmp_path, monkeypatch):
    _set(config_path, "ptp_interface", "enp1s0")
    monkeypatch.setattr(cli, "current_request", lambda systemd, path: None)
    assert cli.main(["--config", str(config_path), "--request", str(tmp_path / "r.json"),
                     "role", "follower", "--leader", "config_path-a"]) == 1


def test_missing_config(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "config", "show"]) == 1
    assert "config init" in capsys.readouterr().err


def test_role_show_without_permission(config_path, tmp_path, monkeypatch, capsys):
    def denied(systemd, path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cli, "current_request", denied)
    assert cli.main(["--config", str(config_path), "--request", "/run/dante-virgil/request.json", "role", "show"]) == 1
    assert "Try again with sudo" in capsys.readouterr().err

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.fixture
def tls_virgil(config_path, tmp_path, monkeypatch):
    tls_dir = tmp_path / "tls"
    monkeypatch.setattr(cli, "ipv4_addresses", lambda name: ["192.168.20.21"])
    assert cli.main(["--config", str(config_path), "--tls-dir", str(tls_dir), "tls", "init"]) == 0
    return config_path, tls_dir


def _group(config_path, tls_dir, *args):
    return cli.main(["--config", str(config_path), "--tls-dir", str(tls_dir), "group", *args])


@needs_openssl
def test_tls_init_keeps_an_existing_certificate(tls_virgil, capsys):
    config_path, tls_dir = tls_virgil
    before = (tls_dir / "cert.pem").read_text()
    assert cli.main(["--config", str(config_path), "--tls-dir", str(tls_dir), "tls", "init"]) == 0
    assert (tls_dir / "cert.pem").read_text() == before
    assert "Keeping" in capsys.readouterr().out


@needs_openssl
def test_group_create_and_show(tls_virgil, capsys):
    config_path, tls_dir = tls_virgil
    assert _group(config_path, tls_dir, "create") == 0
    assert _group(config_path, tls_dir, "create") == 1
    config = config_file.load(config_path)
    assert list(config.group.members) == [config.id]
    capsys.readouterr()
    assert _group(config_path, tls_dir, "show") == 0
    assert "(this Virgil)" in capsys.readouterr().out


@needs_openssl
def test_invite_needs_a_group_and_a_management_interface(tls_virgil):
    config_path, tls_dir = tls_virgil
    assert _group(config_path, tls_dir, "invite") == 1
    _group(config_path, tls_dir, "create")
    assert _group(config_path, tls_dir, "invite") == 1
    _set(config_path, "mgmt_interface", "enp1s0")
    assert _group(config_path, tls_dir, "invite") == 0


@needs_openssl
def test_invite_token(tls_virgil, capsys):
    config_path, tls_dir = tls_virgil
    _group(config_path, tls_dir, "create")
    _set(config_path, "mgmt_interface", "enp1s0")
    capsys.readouterr()
    assert _group(config_path, tls_dir, "invite", "--minutes", "5") == 0
    token = InviteToken.decode(capsys.readouterr().out.strip())
    config = config_file.load(config_path)
    assert token.group_id == config.group.id
    assert token.addresses == ("192.168.20.21",)
    assert token.fingerprint == tls.fingerprint((tls_dir / "cert.pem").read_text())
    assert len(config.group.invites) == 1
