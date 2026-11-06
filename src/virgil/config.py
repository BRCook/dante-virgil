"""The box's config file.

This holds only the settings the daemon core needs so far. Group settings,
the web password and tuning will be added as those parts are built.
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

DEFAULT_PATH = Path("/etc/dante-virgil/virgil.toml")


class ConfigError(Exception):
    pass


@dataclass
class DeviceSettings:
    """Settings for one local Dante device, keyed by its MAC address."""

    native_ptpv2: bool = False
    aes67: bool = False


@dataclass
class Config:
    id: str
    name: str
    ptp_interface: str | None = None
    nic_profile: str | None = None
    mgmt_interface: str | None = None
    trim_ppb: int = 0
    devices: dict[str, DeviceSettings] = field(default_factory=dict)


def new_config() -> Config:
    """A fresh config for a newly installed box."""
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
    return Config(
        id=_expect(data, "id", str),
        name=_expect(data, "name", str),
        ptp_interface=_expect(data, "ptp_interface", str, optional=True),
        nic_profile=_expect(data, "nic_profile", str, optional=True),
        mgmt_interface=_expect(data, "mgmt_interface", str, optional=True),
        trim_ppb=_expect(data, "trim_ppb", int, optional=True) or 0,
        devices=devices,
    )


def to_dict(config: Config) -> dict[str, Any]:
    # TOML has no null, so unset settings are left out.
    data: dict[str, Any] = {"id": config.id, "name": config.name}
    for key in ("ptp_interface", "nic_profile", "mgmt_interface"):
        value = getattr(config, key)
        if value is not None:
            data[key] = value
    data["trim_ppb"] = config.trim_ppb
    if config.devices:
        data["devices"] = {
            mac: {"native_ptpv2": settings.native_ptpv2, "aes67": settings.aes67}
            for mac, settings in sorted(config.devices.items())
        }
    return data


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
