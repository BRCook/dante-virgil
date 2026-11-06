"""Finding other Virgils on the management network, over mDNS.

Each Virgil advertises itself as `_dante-virgil._tcp`, on its management
interface's addresses only, with its Virgil ID, version, ports and group ID.
Every Virgil it hears is reported through a callback, members or not, so the
web UI can show groups that a new Virgil could join.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from virgil.heartbeat import VIRGIL_ID

SERVICE_TYPE = "_dante-virgil._tcp.local."
log = logging.getLogger("virgild.discovery")


@dataclass(frozen=True)
class Advert:
    id: str
    name: str
    version: str
    group_id: str | None
    public_port: int
    group_port: int
    addresses: tuple[str, ...]


def properties(virgil_id: str, name: str, version: str, group_id: str | None,
               public_port: int, group_port: int) -> dict[str, str]:
    return {
        "id": virgil_id,
        "name": name,
        "v": version,
        "g": group_id or "",
        "pp": str(public_port),
        "gp": str(group_port),
    }


def parse(props: dict[bytes, bytes | None], addresses: list[str]) -> Advert | None:
    """Read another Virgil's advert, or None if it isn't a valid one."""
    def text(key: str) -> str:
        value = props.get(key.encode())
        return value.decode(errors="replace") if value else ""

    virgil_id = text("id")
    if not VIRGIL_ID.match(virgil_id) or not addresses:
        return None
    try:
        public_port, group_port = int(text("pp")), int(text("gp"))
    except ValueError:
        return None
    group_id = text("g") or None
    return Advert(virgil_id, text("name")[:64], text("v")[:32], group_id, public_port, group_port, tuple(addresses))


class Discovery:
    def __init__(self, on_advert: Callable[[Advert], None]):
        self._on_advert = on_advert
        self._zeroconf: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._info: AsyncServiceInfo | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self, addresses: list[str], instance: str, hostname: str, port: int,
                    props: dict[str, str]) -> None:
        self._zeroconf = AsyncZeroconf(interfaces=addresses or None, ip_version=IPVersion.V4Only)
        self._info = AsyncServiceInfo(
            SERVICE_TYPE,
            f"{instance}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in addresses],
            port=port,
            properties=props,
            server=f"{hostname}.local.",
        )
        await self._zeroconf.async_register_service(self._info, allow_name_change=True)
        self._browser = AsyncServiceBrowser(self._zeroconf.zeroconf, [SERVICE_TYPE], handlers=[self._changed])

    async def update(self, props: dict[str, str]) -> None:
        """Change what this Virgil advertises, such as after joining a group."""
        if self._zeroconf is None or self._info is None:
            return
        self._info = AsyncServiceInfo(
            SERVICE_TYPE, self._info.name, addresses=self._info.addresses, port=self._info.port,
            properties=props, server=self._info.server,
        )
        await self._zeroconf.async_update_service(self._info)

    def _changed(self, zeroconf: Zeroconf, service_type: str, name: str,
                 state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        task = asyncio.ensure_future(self._resolve(zeroconf, service_type, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000):
            return
        advert = parse(info.properties or {}, info.parsed_addresses(IPVersion.V4Only))
        if advert is not None:
            self._on_advert(advert)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.async_cancel()
        if self._zeroconf is not None:
            if self._info is not None:
                await self._zeroconf.async_unregister_service(self._info)
            await self._zeroconf.async_close()
