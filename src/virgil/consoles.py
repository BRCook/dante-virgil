"""Monitoring and looking after the local Dante devices, through netaudio.

Virgil runs your fork of netaudio's daemon (dante-virgil-netaudio.service),
listening on loopback only, and reads from its HTTP API:

  GET  /devices                    every device netaudio knows about
  GET  /diagnostics/<server name>  clock and receive statistics for one device
  POST /set-clock-configuration    {"device", "changes": {"ptpv2_enabled": true}}
  POST /set-aes67                  {"device", "enabled": true}

Which devices are local comes from PTP watching: a device is local if its
address has sent PTP on this Virgil's PTP interface.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from virgil.config import DeviceSettings

log = logging.getLogger("virgild.consoles")

NETAUDIO_URL = "http://127.0.0.1:4780"
POLL_INTERVAL = 10.0
DIAGNOSTICS_INTERVAL = 60.0
# The least time between attempts to change one setting on one device.
CHANGE_INTERVAL = 30.0
# How long a device with PTPv2 on gets to start following this Virgil, or to
# synchronise, before a warning.
FOLLOW_GRACE = 20.0


def normalise_mac(mac: Any) -> str | None:
    """'001DC1AABBCC', '00-1d-c1-aa-bb-cc' and so on, as '00:1d:c1:aa:bb:cc'."""
    if not isinstance(mac, str):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if len(digits) != 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


@dataclass
class DanteDevice:
    """What Virgil reports about one local Dante device."""

    server_name: str
    name: str
    address: str
    mac: str | None
    model: str | None
    ptpv2: bool | None
    aes67: bool | None
    clock_offset_ppb: int | None
    # AES67 mode as configured; it may only take effect after the device restarts.
    aes67_configured: bool | None = None
    # The device's own view of its clock: the PTPv2 grandmaster it follows, and its servo.
    grandmaster: str | None = None
    servo: str | None = None
    follows_this_virgil: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "mac": self.mac,
            "model": self.model,
            "ptpv2": self.ptpv2,
            "aes67": self.aes67,
            "clock_offset_ppb": self.clock_offset_ppb,
            "follows_this_virgil": self.follows_this_virgil,
            "grandmaster": self.grandmaster,
            "servo": self.servo,
            "warnings": list(self.warnings),
        }


def find_mac(record: Any, depth: int = 0) -> str | None:
    """A device's MAC address from netaudio's record, wherever in it netaudio puts it."""
    if depth > 4:
        return None
    if isinstance(record, dict):
        for key in ("mac_address", "mac"):
            value = record.get(key)
            # netaudio sometimes gives the 16-digit PTP clock identity made from the MAC.
            mac = normalise_mac(value) or (mac_from_clock_identity(value) if isinstance(value, str) else None)
            if mac:
                return mac
        children = record.values()
    elif isinstance(record, list):
        children = record
    else:
        return None
    for child in children:
        if isinstance(child, (dict, list)):
            mac = find_mac(child, depth + 1)
            if mac:
                return mac
    return None


def mac_from_clock_identity(identity: str | None) -> str | None:
    """A PTP clock identity made from a MAC address (ff:fe inserted in the middle) back to the MAC."""
    if not identity:
        return None
    digits = identity.replace(":", "").lower()
    if len(digits) != 16 or digits[6:10] != "fffe":
        return None
    return normalise_mac(digits[:6] + digits[10:])


def clock_identity(value: Any) -> str | None:
    """A PTP clock identity, such as '8ca682fffe713bb6', as '8c:a6:82:ff:fe:71:3b:b6'."""
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if len(digits) != 16:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 16, 2))


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def parse_devices(data: Any) -> list[DanteDevice]:
    """Read netaudio's /devices response, skipping anything without a usable address."""
    if not isinstance(data, dict):
        return []
    devices = []
    for server_name, record in data.items():
        if not isinstance(record, dict):
            continue
        address = record.get("ipv4")
        if not isinstance(address, str) or not address or address == "None":
            continue
        clock_status = record.get("clock_status") if isinstance(record.get("clock_status"), dict) else {}
        offset = record.get("clock_frequency_offset_parts_per_billion")
        model = record.get("product_name") or record.get("model")
        devices.append(
            DanteDevice(
                server_name=str(server_name),
                name=str(record.get("name") or server_name),
                address=address,
                mac=find_mac(record),
                model=str(model) if model else None,
                ptpv2=_bool(clock_status.get("ptpv2_enabled")),
                aes67=_bool(record.get("aes67_current")),
                clock_offset_ppb=offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
                aes67_configured=_bool(record.get("aes67_configured")),
                grandmaster=clock_identity(clock_status.get("ptpv2_grandmaster_identity")),
                servo=str(clock_status["servo_state"]) if isinstance(clock_status.get("servo_state"), str) else None,
            )
        )
    return devices


class NetaudioClient:
    def __init__(self, base_url: str = NETAUDIO_URL, timeout: float = 5.0):
        self.base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.request(method, self.base_url + path, json=body) as reply:
                data = await reply.json(content_type=None)
                if reply.status != 200:
                    message = data.get("error") if isinstance(data, dict) else None
                    raise aiohttp.ClientResponseError(reply.request_info, (), status=reply.status,
                                                      message=str(message or reply.status))
                return data

    async def devices(self) -> Any:
        return await self._request("GET", "/devices")

    async def diagnostics(self, server_name: str) -> Any:
        return await self._request("GET", f"/diagnostics/{quote(server_name, safe='')}?receiver_history=0")

    async def issues(self) -> Any:
        return await self._request("GET", "/issues?state=open&limit=200")

    async def set_ptpv2(self, server_name: str, enabled: bool) -> Any:
        return await self._request("POST", "/set-clock-configuration",
                                   {"device": server_name, "changes": {"ptpv2_enabled": enabled}})

    async def set_aes67(self, server_name: str, enabled: bool) -> Any:
        return await self._request("POST", "/set-aes67", {"device": server_name, "enabled": enabled})


@dataclass
class Consoles:
    """Keeps track of the local Dante devices, and keeps their toggled settings on."""

    client: NetaudioClient
    # Addresses PTP watching has heard from, and whether each is following this Virgil.
    local: Callable[[], dict[str, bool]]
    settings: Callable[[], dict[str, DeviceSettings]]
    clock: Callable[[], float] = time.monotonic
    # The PTP clock identity PTP watching has seen from an address, if any.
    clock_identity: Callable[[str], str | None] = lambda address: None
    # This Virgil's own grandmaster identity, and the other members' (identity to name).
    own_identity: Callable[[], str | None] = lambda: None
    member_identities: Callable[[], dict[str, str]] = dict
    devices: dict[str, DanteDevice] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    reachable: bool | None = None
    _attempts: dict[tuple[str, str], float] = field(default_factory=dict)
    _ptpv2_since: dict[str, float] = field(default_factory=dict)
    _last_diagnostics: float = float("-inf")

    async def poll(self) -> None:
        try:
            data = await self.client.devices()
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as error:
            if self.reachable is not False:
                log.warning("Can't reach netaudio's daemon: %s", error or type(error).__name__)
            self.reachable = False
            # Old readings would look like consoles that have stopped changing; drop them.
            self.devices = {}
            self.diagnostics = {}
            return
        if self.reachable is not True:
            log.info("Connected to netaudio's daemon")
        self.reachable = True

        now = self.clock()
        local = self.local()
        current = {}
        for device in parse_devices(data):
            if device.address not in local:
                continue
            own = self.own_identity()
            # The device's own word is best; the Delay Requests PTP watching hears are the fallback.
            if device.grandmaster is not None and own is not None:
                device.follows_this_virgil = device.grandmaster == own
            else:
                device.follows_this_virgil = local[device.address]
            if device.mac is None:
                device.mac = mac_from_clock_identity(self.clock_identity(device.address))
            if device.address not in self.devices:
                log.info("Local Dante device: %s (%s) at %s", device.name, device.model or "unknown model",
                         device.address)
            current[device.address] = device
        self.devices = current
        settings = self.settings()
        for device in current.values():
            await self._look_after(device, settings.get(device.mac or ""), now)
            device.warnings = self._warnings(device, now)

        if now - self._last_diagnostics >= DIAGNOSTICS_INTERVAL:
            self._last_diagnostics = now
            for device in current.values():
                try:
                    self.diagnostics[device.address] = await self.client.diagnostics(device.server_name)
                except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                    self.diagnostics.pop(device.address, None)
        for address in list(self.diagnostics):
            if address not in current:
                del self.diagnostics[address]

    async def _look_after(self, device: DanteDevice, wanted: DeviceSettings | None, now: float) -> None:
        """Turn on whichever of PTPv2 and AES67 the device's toggles ask for, if they're off."""
        if wanted is None:
            return
        if wanted.native_ptpv2 and device.ptpv2 is False:
            await self._change(device, "ptpv2", now, self.client.set_ptpv2)
        configured = device.aes67_configured if device.aes67_configured is not None else device.aes67
        if wanted.aes67 and configured is False:
            await self._change(device, "aes67", now, self.client.set_aes67)

    async def _change(self, device: DanteDevice, setting: str, now: float, action) -> None:
        key = (device.address, setting)
        if now - self._attempts.get(key, float("-inf")) < CHANGE_INTERVAL:
            return
        self._attempts[key] = now
        label = "native PTPv2" if setting == "ptpv2" else "AES67 mode"
        try:
            await action(device.server_name, True)
            log.info("Turned on %s for %s", label, device.name)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as error:
            log.warning("Couldn't turn on %s for %s: %s", label, device.name, error or type(error).__name__)

    def _warnings(self, device: DanteDevice, now: float) -> list[str]:
        warnings = []
        if device.aes67_configured and device.aes67 is False:
            warnings.append(f"{device.name} has AES67 mode set, but needs restarting for it to take effect")
        return warnings + self._ptp_warnings(device, now)

    def _ptp_warnings(self, device: DanteDevice, now: float) -> list[str]:
        if device.ptpv2 is False and device.aes67 is not True:
            self._ptpv2_since.pop(device.address, None)
            return [f"{device.name} has PTPv2 off, so it can't follow this Virgil"]
        if not (device.ptpv2 or device.aes67):
            return []
        since = self._ptpv2_since.setdefault(device.address, now)
        if now - since < FOLLOW_GRACE:
            return []
        if not device.follows_this_virgil:
            own = self.own_identity()
            if device.grandmaster is not None and own is not None and device.grandmaster != own:
                other = self.member_identities().get(device.grandmaster)
                source = f"{other}'s grandmaster" if other else f"another grandmaster ({device.grandmaster})"
                return [f"{device.name} is following {source}, not this Virgil's"]
            return [f"{device.name} has PTPv2 on, but isn't following this Virgil"]
        if device.servo is not None and device.servo != "synchronized":
            return [f"{device.name}'s clock isn't synchronised yet (its servo is {device.servo})"]
        return []

    def summaries(self) -> list[dict[str, Any]]:
        return [d.summary() for d in sorted(self.devices.values(), key=lambda d: d.address)]
