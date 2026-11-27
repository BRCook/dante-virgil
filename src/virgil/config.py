"""This Virgil's config file: /etc/dante-virgil/virgil.toml.

Everything one Virgil is set up with: its ID and name, its interfaces, its trim and tuning, the web UI's
password (as a hash), its local Dante devices' settings, and its group (the members and their certificates,
outstanding invites, and removals). The design document's "Configuration" describes each setting.

virgild and virgilctl both read and write this file (see Daemon.save for how they stay out of each other's way).
"""

from __future__ import annotations

import socket
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from virgil.files import atomic_write
from virgil.group import Group, Member, Removed

DEFAULT_PATH = Path("/etc/dante-virgil/virgil.toml")


class ConfigError(Exception):
    pass


PTPV1_MODES = ("fallback", "always", "off", "only")


def _ptpv1_mode(value) -> str:
    if value not in PTPV1_MODES:
        raise ConfigError(f"ptpv1 must be one of {', '.join(PTPV1_MODES)}")
    return value


@dataclass
class DeviceSettings:
    """Settings for one local Dante device, keyed by its MAC address."""

    native_ptpv2: bool = False
    aes67: bool = False


@dataclass(frozen=True)
class NtpKey:
    """The group's key for authenticating NTP between members (see virgil.shared and the design document's
    "Authenticated NTP"), and the one it replaced, which the leader goes on accepting while followers move over."""

    id: int
    key: str  # 64 hex digits: 32 random bytes
    previous_id: int | None = None
    previous_key: str | None = None


# A chrony key ID, and its key: 32 bytes, in hex.
NTP_KEY_IDS = (1, 65535)
NTP_KEY_HEX = 64


def valid_ntp_key(key: NtpKey) -> bool:
    def one(key_id: Any, hex_key: Any) -> bool:
        return (isinstance(key_id, int) and not isinstance(key_id, bool) and NTP_KEY_IDS[0] <= key_id <= NTP_KEY_IDS[1]
                and isinstance(hex_key, str) and len(hex_key) == NTP_KEY_HEX
                and all(c in "0123456789abcdef" for c in hex_key))
    if not one(key.id, key.key):
        return False
    if key.previous_id is None and key.previous_key is None:
        return True
    return one(key.previous_id, key.previous_key) and key.previous_id != key.id


@dataclass
class Config:
    id: str
    name: str
    ptp_interface: str | None = None
    nic_profile: str | None = None
    mgmt_interface: str | None = None
    trim_ppb: int = 0
    # True to have virgild only watch the election, leaving roles to virgilctl.
    observe_only: bool = False
    # While leading, nudge the trim toward the consoles' own rate.
    auto_trim: bool = True
    # When auto_trim was last changed: shared across the group, the latest change winning (see virgil.shared).
    auto_trim_changed: float = 0.0
    # Have chrony use the management interface's hardware timestamps, where it safely can.
    ntp_hwtimestamp: bool = True
    # Serving PTPv1 to Dante devices: "fallback" (only when no local device bridges this Virgil's
    # PTPv2 into PTPv1), "always", "off", or "only" (instead of PTPv2: ptp4l doesn't run).
    ptpv1: str = "fallback"
    # Measure the PTP interface's transmit timestamp slots during a setup from scratch (see virgil.tsprobe).
    probe_timestamps: bool = True
    # Whether to ask GitHub once a day for a newer Virgil, and say so (see virgil.updates). Nothing is installed.
    check_updates: bool = True
    # phc2sys's servo on a follower: how firmly the PHC follows the system clock (see the design document's
    # "Follower"). Gentler values make the PHC's rate steadier, at the cost of a larger offset from the system clock.
    phc2sys_kp: float = 0.03
    phc2sys_ki: float = 0.0005
    # How many measurements chrony, on a follower, merges into each sample, by their median (1: none; 8 by default). Chrony keeps
    # at most 64 samples, so merging makes its history longer and cleaner, and its corrections smaller and smoother,
    # at the cost of following real changes (such as the leader's crystal drifting) more slowly.
    chrony_filter: int = 8
    # The web UI's password, as a scrypt hash. None means the web UI is read-only.
    web_password: str | None = None
    # When the password was last changed: shared across the group too.
    web_password_changed: float = 0.0
    # Features switched on for the whole group, once every member supports them (see virgil.features). Shared
    # across the group, and only ever added to.
    features: list[str] = field(default_factory=list)
    features_changed: float = 0.0
    # The group's NTP key (see NtpKey), shared across the group, the latest change winning.
    ntp_key: NtpKey | None = None
    ntp_key_changed: float = 0.0
    devices: dict[str, DeviceSettings] = field(default_factory=dict)
    group: Group | None = None


def new_config() -> Config:
    """A fresh config for a newly installed Virgil."""
    return Config(id=uuid.uuid4().hex, name=socket.gethostname())


def _expect(data: dict[str, Any], key: str, kind: type | tuple[type, ...], optional: bool = False) -> Any:
    if key not in data:
        if optional:
            return None
        raise ConfigError(f"missing setting: {key}")
    value = data[key]
    # bool is a subclass of int, so don't let true/false pass as a number.
    if isinstance(value, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise ConfigError(f"{key} must be {getattr(kind, '__name__', kind)}, not a boolean")
    if not isinstance(value, kind):
        raise ConfigError(f"{key} has the wrong type: {type(value).__name__}")
    return value


def from_dict(data: dict[str, Any]) -> Config:
    devices = {}
    for mac, settings in (_expect(data, "devices", dict, optional=True) or {}).items():
        if not isinstance(settings, dict):
            raise ConfigError(f"devices.{mac} must be a table")
        devices[mac.lower()] = DeviceSettings(
            native_ptpv2=_expect(settings, "native_ptpv2", bool, optional=True) or False,
            aes67=_expect(settings, "aes67", bool, optional=True) or False,
        )
    group = None
    group_data = _expect(data, "group", dict, optional=True)
    if group_data is not None:
        members = {}
        for virgil_id, member in (_expect(group_data, "members", dict, optional=True) or {}).items():
            if not isinstance(member, dict):
                raise ConfigError(f"group.members.{virgil_id} must be a table")
            changed = member.get("changed", 0.0)
            if isinstance(changed, bool) or not isinstance(changed, (int, float)):
                raise ConfigError(f"group.members.{virgil_id}.changed must be a time")
            members[virgil_id] = Member(name=_expect(member, "name", str), cert=_expect(member, "cert", str),
                                        changed=float(changed))
        removed = {}
        for virgil_id, entry in (_expect(group_data, "removed", dict, optional=True) or {}).items():
            if not isinstance(entry, dict) or isinstance(entry.get("changed"), bool) \
                    or not isinstance(entry.get("changed"), (int, float)):
                raise ConfigError(f"group.removed.{virgil_id} must have a time")
            removed[virgil_id] = Removed(float(entry["changed"]), entry.get("fingerprint"), entry.get("by"))
        invites = {}
        for key, expires in (_expect(group_data, "invites", dict, optional=True) or {}).items():
            if isinstance(expires, bool) or not isinstance(expires, int):
                raise ConfigError("group.invites must map to expiry times")
            invites[key] = expires
        group = Group(id=_expect(group_data, "id", str), members=members, invites=invites, removed=removed)

    return Config(
        id=_expect(data, "id", str),
        name=_expect(data, "name", str),
        ptp_interface=_expect(data, "ptp_interface", str, optional=True),
        nic_profile=_expect(data, "nic_profile", str, optional=True),
        mgmt_interface=_expect(data, "mgmt_interface", str, optional=True),
        trim_ppb=_expect(data, "trim_ppb", int, optional=True) or 0,
        observe_only=_expect(data, "observe_only", bool, optional=True) or False,
        auto_trim=_expect(data, "auto_trim", bool, optional=True) is not False,
        ntp_hwtimestamp=_expect(data, "ntp_hwtimestamp", bool, optional=True) is not False,
        ptpv1=_ptpv1_mode(data.get("ptpv1", "fallback")),
        probe_timestamps=_expect(data, "probe_timestamps", bool, optional=True) is not False,
        check_updates=_expect(data, "check_updates", bool, optional=True) is not False,
        web_password=_expect(data, "web_password", str, optional=True),
        auto_trim_changed=_time(data, "auto_trim_changed"),
        phc2sys_kp=_servo(data, "phc2sys_kp", 0.03),
        phc2sys_ki=_servo(data, "phc2sys_ki", 0.0005),
        chrony_filter=_whole(data, "chrony_filter", 8),
        web_password_changed=_time(data, "web_password_changed"),
        features=_features(data),
        features_changed=_time(data, "features_changed"),
        ntp_key=_ntp_key(data),
        ntp_key_changed=_time(data, "ntp_key_changed"),
        devices=devices,
        group=group,
    )


def to_dict(config: Config) -> dict[str, Any]:
    # TOML has no null, so unset settings are left out.
    data: dict[str, Any] = {"id": config.id, "name": config.name}
    for key in ("ptp_interface", "nic_profile", "mgmt_interface", "web_password"):
        value = getattr(config, key)
        if value is not None:
            data[key] = value
    data["trim_ppb"] = config.trim_ppb
    data["observe_only"] = config.observe_only
    data["auto_trim"] = config.auto_trim
    data["auto_trim_changed"] = config.auto_trim_changed
    data["web_password_changed"] = config.web_password_changed
    data["ntp_hwtimestamp"] = config.ntp_hwtimestamp
    data["ptpv1"] = config.ptpv1
    data["probe_timestamps"] = config.probe_timestamps
    data["check_updates"] = config.check_updates
    data["phc2sys_kp"] = config.phc2sys_kp
    data["phc2sys_ki"] = config.phc2sys_ki
    data["chrony_filter"] = config.chrony_filter
    data["features"] = sorted(config.features)
    data["features_changed"] = config.features_changed
    if config.ntp_key is not None:
        key = config.ntp_key
        data["ntp_key"] = {"id": key.id, "key": key.key, **({"previous_id": key.previous_id, "previous_key":
                                                             key.previous_key} if key.previous_key else {})}
    data["ntp_key_changed"] = config.ntp_key_changed
    if config.devices:
        data["devices"] = {
            mac: {"native_ptpv2": settings.native_ptpv2, "aes67": settings.aes67}
            for mac, settings in sorted(config.devices.items())
        }
    if config.group is not None:
        data["group"] = {
            "id": config.group.id,
            "members": {
                virgil_id: {"name": member.name, "cert": member.cert, "changed": member.changed}
                for virgil_id, member in sorted(config.group.members.items())
            },
            "invites": dict(sorted(config.group.invites.items())),
        }
        if config.group.removed:
            data["group"]["removed"] = {
                virgil_id: {"changed": entry.changed, **({"fingerprint": entry.fingerprint} if entry.fingerprint else {}),
                            **({"by": entry.by} if entry.by else {})}
                for virgil_id, entry in sorted(config.group.removed.items())
            }
    return data


def _servo(data: dict, key: str, default: float) -> float:
    from virgil.request import TUNING_LIMITS

    value = data.get(key, default)
    low, high = TUNING_LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ConfigError(f"{key} must be a number from {low} to {high}")
    return float(value)


def _whole(data: dict, key: str, default: int) -> int:
    from virgil.request import TUNING_LIMITS

    value = data.get(key, default)
    low, high = TUNING_LIMITS[key.removeprefix("chrony_")]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{key} must be a whole number from {low} to {high}")
    return value


def _features(data: dict) -> list[str]:
    from virgil.features import FEATURES

    value = data.get("features", [])
    if not isinstance(value, list) or not all(isinstance(f, str) for f in value):
        raise ConfigError("features must be a list of names")
    return sorted({f for f in value if f in FEATURES})  # one a later Virgil knows of, and this one doesn't, is left out


def _ntp_key(data: dict) -> NtpKey | None:
    value = data.get("ntp_key")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("ntp_key must be a table")
    key = NtpKey(value.get("id"), value.get("key"), value.get("previous_id"), value.get("previous_key"))
    if not valid_ntp_key(key):
        raise ConfigError("ntp_key must have an ID from 1 to 65535 and a key of 64 hex digits (and the same for the "
                          "previous key, if there is one)")
    return key


def _time(data: dict, key: str) -> float:
    value = data.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a time")
    return float(value)


def load(path: Path = DEFAULT_PATH) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    return from_dict(data)


def save(config: Config, path: Path = DEFAULT_PATH) -> None:
    """Write the config atomically, so a crash never leaves half a file."""
    atomic_write(path, tomli_w.dumps(to_dict(config)).encode(), mode=0o640)
