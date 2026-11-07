"""Heartbeats: what each Virgil tells the others about itself, once a second."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from virgil.election import PeerView, State

HEARTBEAT_PATH = "/group/v1/heartbeat"
VIRGIL_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_COIN = 2**64 - 1
PTP_IDENTITY = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){7}$")
MAX_WARNINGS = 8
MAX_WARNING_LENGTH = 200
MAX_DEVICES = 16
MAX_DEVICES_BYTES = 16 * 1024


class HeartbeatError(Exception):
    pass


@dataclass(frozen=True)
class Heartbeat:
    id: str
    name: str
    version: str
    state: State
    coin: int
    leader: str | None
    lost_leader: bool
    handover_to: str | None
    # True while virgild only watches the election, without changing roles.
    observing: bool = False
    # A leader that had heard from the group before it started leading.
    established: bool = False
    # This Virgil's PTP grandmaster identity, once known, and any PTP warnings.
    ptp_identity: str | None = None
    ptp_warnings: tuple[str, ...] = ()
    # Summaries of this Virgil's local Dante devices, from netaudio.
    dante_devices: tuple[dict[str, Any], ...] = ()
    # What chrony and phc2sys are doing (see virgil.clocks).
    clock: dict[str, Any] | None = None

    def encode(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "state": self.state.value,
            "coin": self.coin,
            "leader": self.leader,
            "lost_leader": self.lost_leader,
            "handover_to": self.handover_to,
            "observing": self.observing,
            "established": self.established,
            "ptp_identity": self.ptp_identity,
            "ptp_warnings": list(self.ptp_warnings),
            "dante_devices": list(self.dante_devices),
            "clock": self.clock,
        }

    def view(self, heard_at: float) -> PeerView:
        return PeerView(self.id, self.state, self.coin, self.leader, self.lost_leader, self.handover_to, heard_at,
                        self.established)


def _optional_id(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not VIRGIL_ID.match(value):
        raise HeartbeatError(f"bad {key}")
    return value


def decode(data: Any) -> Heartbeat:
    if not isinstance(data, dict):
        raise HeartbeatError("a heartbeat must be an object")
    virgil_id, name, version = data.get("id"), data.get("name"), data.get("version")
    if not isinstance(virgil_id, str) or not VIRGIL_ID.match(virgil_id):
        raise HeartbeatError("bad id")
    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        raise HeartbeatError("bad name")
    if not isinstance(version, str) or len(version) > 32:
        raise HeartbeatError("bad version")
    try:
        state = State(data.get("state"))
    except ValueError as error:
        raise HeartbeatError("bad state") from error
    coin = data.get("coin")
    if isinstance(coin, bool) or not isinstance(coin, int) or not 0 <= coin <= MAX_COIN:
        raise HeartbeatError("bad coin")
    lost, observing = data.get("lost_leader"), data.get("observing", False)
    established = data.get("established", False)
    if not all(isinstance(flag, bool) for flag in (lost, observing, established)):
        raise HeartbeatError("bad flags")
    ptp_identity = data.get("ptp_identity")
    if ptp_identity is not None and (not isinstance(ptp_identity, str) or not PTP_IDENTITY.match(ptp_identity)):
        raise HeartbeatError("bad ptp_identity")
    warnings = data.get("ptp_warnings", [])
    if (not isinstance(warnings, list) or len(warnings) > MAX_WARNINGS
            or not all(isinstance(w, str) and len(w) <= MAX_WARNING_LENGTH for w in warnings)):
        raise HeartbeatError("bad ptp_warnings")
    devices = data.get("dante_devices", [])
    if (not isinstance(devices, list) or len(devices) > MAX_DEVICES
            or not all(isinstance(d, dict) for d in devices)
            or len(json.dumps(devices)) > MAX_DEVICES_BYTES):
        raise HeartbeatError("bad dante_devices")
    clock = data.get("clock")
    if clock is not None and (
        not isinstance(clock, dict) or len(clock) > 16
        or not all(isinstance(k, str) and (v is None or isinstance(v, (str, int, float, bool)))
                   for k, v in clock.items())
        or len(json.dumps(clock)) > 2048
    ):
        raise HeartbeatError("bad clock")
    return Heartbeat(
        id=virgil_id,
        name=name,
        version=version,
        state=state,
        coin=coin,
        leader=_optional_id(data, "leader"),
        lost_leader=lost,
        handover_to=_optional_id(data, "handover_to"),
        observing=observing,
        established=established,
        ptp_identity=ptp_identity,
        ptp_warnings=tuple(warnings),
        dante_devices=tuple(devices),
        clock=clock,
    )
