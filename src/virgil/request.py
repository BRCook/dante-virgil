"""The timing request: everything the daemon asks root to set up, and nothing more.

The daemon runs as an unprivileged user and writes this file. virgil-root runs
as root and reads it, so every field is validated strictly here, and anything
that can be worked out from the hardware (the PHC, the driver) is never taken
from the request at all.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from virgil.generate import Role, Tuning

REQUEST_PATH = Path("/run/dante-virgil/request.json")
MAX_REQUEST_BYTES = 64 * 1024
MAX_ALLOW = 64
MAX_TRIM_PPB = 1_000_000

INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")

# (minimum, maximum) for each tuning field.
TUNING_LIMITS: dict[str, tuple[float, float]] = {
    "maxdelayquant": (0.01, 1.0),
    "minsamples": (1, 1024),
    "maxsamples": (1, 1024),
    "corrtimeratio": (1, 100),
    "maxslewrate": (1, 100_000),
    "phc2sys_kp": (0.0001, 1.0),
    "phc2sys_ki": (0.0, 1.0),
    "log_sync_interval": (-7, 4),
    "log_announce_interval": (-3, 4),
    "log_min_delay_req_interval": (-7, 4),
}


class RequestError(Exception):
    pass


@dataclass(frozen=True)
class TimingRequest:
    role: Role
    ptp_interface: str
    # The leader's address. Required for followers; leaders have none.
    leader: str | None
    # Addresses and networks chrony allows NTP from.
    allow: tuple[str, ...]
    trim_ppb: int = 0
    # Whether to apply the known NIC profile for the PTP interface's driver.
    use_nic_profile: bool = True
    tuning: Tuning = Tuning()


def to_json(request: TimingRequest) -> str:
    data = asdict(request)
    data["role"] = request.role.value
    data["allow"] = list(request.allow)
    return json.dumps(data, indent=2) + "\n"


def _require(data: dict[str, Any], key: str, kind: type) -> Any:
    if key not in data:
        raise RequestError(f"missing {key}")
    value = data[key]
    if isinstance(value, bool) and kind is not bool:
        raise RequestError(f"{key} must not be a boolean")
    if not isinstance(value, kind):
        raise RequestError(f"{key} has the wrong type")
    return value


def _tuning(data: Any) -> Tuning:
    if not isinstance(data, dict):
        raise RequestError("tuning must be an object")
    known = {f.name: f.type for f in fields(Tuning)}
    unknown = set(data) - set(known)
    if unknown:
        raise RequestError(f"unknown tuning settings: {', '.join(sorted(unknown))}")
    values = {}
    defaults = Tuning()
    for name in known:
        value = data.get(name, getattr(defaults, name))
        default = getattr(defaults, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RequestError(f"tuning.{name} must be a number")
        if isinstance(default, int) and not isinstance(value, int):
            raise RequestError(f"tuning.{name} must be a whole number")
        low, high = TUNING_LIMITS[name]
        if not low <= value <= high:
            raise RequestError(f"tuning.{name} must be between {low} and {high}")
        values[name] = value
    tuning = Tuning(**values)
    if tuning.minsamples > tuning.maxsamples:
        raise RequestError("tuning.minsamples must not be more than tuning.maxsamples")
    return tuning


def from_json(text: str | bytes) -> TimingRequest:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RequestError(f"not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise RequestError("the request must be an object")
    unknown = set(data) - {f.name for f in fields(TimingRequest)}
    if unknown:
        raise RequestError(f"unknown settings: {', '.join(sorted(unknown))}")

    try:
        role = Role(_require(data, "role", str))
    except ValueError as error:
        raise RequestError("role must be 'leader' or 'follower'") from error

    interface = _require(data, "ptp_interface", str)
    if not INTERFACE_NAME.match(interface):
        raise RequestError("ptp_interface is not a valid interface name")

    leader = data.get("leader")
    if leader is not None:
        if not isinstance(leader, str):
            raise RequestError("leader must be an address")
        try:
            leader = str(ipaddress.ip_address(leader))
        except ValueError as error:
            raise RequestError("leader must be an IP address") from error
    if role is Role.FOLLOWER and leader is None:
        raise RequestError("a follower needs a leader")
    if role is Role.LEADER and leader is not None:
        raise RequestError("a leader doesn't have a leader")

    allow_list = _require(data, "allow", list)
    if len(allow_list) > MAX_ALLOW:
        raise RequestError(f"allow has more than {MAX_ALLOW} entries")
    allow = []
    for entry in allow_list:
        if not isinstance(entry, str):
            raise RequestError("allow entries must be addresses or networks")
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise RequestError(f"allow entry {entry!r} is not an address or network") from error
        allow.append(str(network.network_address) if network.num_addresses == 1 else str(network))

    trim = data.get("trim_ppb", 0)
    if isinstance(trim, bool) or not isinstance(trim, int) or abs(trim) > MAX_TRIM_PPB:
        raise RequestError(f"trim_ppb must be a whole number within ±{MAX_TRIM_PPB}")

    use_profile = data.get("use_nic_profile", True)
    if not isinstance(use_profile, bool):
        raise RequestError("use_nic_profile must be true or false")

    return TimingRequest(
        role=role,
        ptp_interface=interface,
        leader=leader,
        allow=tuple(allow),
        trim_ppb=trim,
        use_nic_profile=use_profile,
        tuning=_tuning(data.get("tuning", {})),
    )
