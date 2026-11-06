import pytest

from virgil import config
from virgil.config import Config, ConfigError, DeviceSettings
from virgil.group import Group, Member


def test_round_trip(tmp_path):
    path = tmp_path / "virgil.toml"
    original = Config(
        id="abc123",
        name="Virgil A",
        ptp_interface="enp1s0",
        nic_profile="igc",
        mgmt_interface="enp5s0",
        trim_ppb=21500,
        devices={"aa:bb:cc:dd:ee:ff": DeviceSettings(native_ptpv2=True)},
    )
    config.save(original, path)
    assert config.load(path) == original


def test_unset_settings_are_left_out(tmp_path):
    path = tmp_path / "virgil.toml"
    config.save(Config(id="abc", name="Virgil B"), path)
    text = path.read_text()
    assert "ptp_interface" not in text and "devices" not in text
    assert config.load(path) == Config(id="abc", name="Virgil B")


def test_new_config_has_an_id():
    a, b = config.new_config(), config.new_config()
    assert a.id != b.id and a.name


def test_mac_addresses_are_lowercased():
    loaded = config.from_dict({"id": "x", "name": "y", "devices": {"AA:BB:CC:DD:EE:FF": {"aes67": True}}})
    assert loaded.devices == {"aa:bb:cc:dd:ee:ff": DeviceSettings(aes67=True)}


@pytest.mark.parametrize(
    "data",
    [
        {"name": "y"},
        {"id": "x", "name": "y", "trim_ppb": "lots"},
        {"id": "x", "name": "y", "trim_ppb": True},
        {"id": "x", "name": "y", "devices": {"aa": "on"}},
        {"id": "x", "name": "y", "devices": {"aa": {"native_ptpv2": "yes"}}},
    ],
)
def test_bad_config_is_rejected(data):
    with pytest.raises(ConfigError):
        config.from_dict(data)


def test_bad_toml_is_rejected(tmp_path):
    path = tmp_path / "virgil.toml"
    path.write_text("id = \n")
    with pytest.raises(ConfigError):
        config.load(path)


def test_save_leaves_no_temporary_files(tmp_path):
    path = tmp_path / "virgil.toml"
    config.save(Config(id="a", name="b"), path)
    config.save(Config(id="a", name="c"), path)
    assert [p.name for p in tmp_path.iterdir()] == ["virgil.toml"]


def test_group_round_trip(tmp_path):
    path = tmp_path / "virgil.toml"
    cert = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
    original = Config(
        id="a",
        name="Virgil A",
        group=Group(id="f" * 32, members={"a": Member("Virgil A", cert), "b": Member("Virgil B", cert)}, invites={"h": 99}),
    )
    config.save(original, path)
    assert config.load(path) == original


def test_bad_group_is_rejected():
    with pytest.raises(ConfigError):
        config.from_dict({"id": "x", "name": "y", "group": {"id": "g", "members": {"a": {"name": "A"}}}})
