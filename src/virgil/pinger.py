"""Small, frequent pings to the other Virgils, for the link's round trip and jitter.

Uses an unprivileged ICMP socket where the kernel allows one, and a raw one
otherwise (virgild has CAP_NET_RAW for PTP watching). Replies are matched by
sequence number; one that hasn't come back within a second counts as lost.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from collections.abc import Callable

log = logging.getLogger("virgild.pinger")

INTERVAL = 0.25
TIMEOUT = 1.0
ECHO_REQUEST, ECHO_REPLY = 8, 0


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def echo_request(identifier: int, sequence: int, payload: bytes = b"virgil") -> bytes:
    header = struct.pack("!BBHHH", ECHO_REQUEST, 0, 0, identifier, sequence)
    checksum = _checksum(header + payload)
    return struct.pack("!BBHHH", ECHO_REQUEST, 0, checksum, identifier, sequence) + payload


def parse_reply(data: bytes) -> int | None:
    """The sequence number of an echo reply, with or without an IPv4 header in front."""
    if data and data[0] >> 4 == 4 and len(data) >= 20:
        data = data[(data[0] & 0x0F) * 4:]  # a raw socket includes the IP header
    if len(data) < 8 or data[0] != ECHO_REPLY:
        return None
    return struct.unpack_from("!H", data, 6)[0]


class Pinger:
    def __init__(self, on_result: Callable[[str, float | None], None], clock: Callable[[], float] = time.monotonic):
        self.on_result = on_result  # (address, round trip in ns, or None if lost)
        self.clock = clock
        self.targets: set[str] = set()
        self._sock: socket.socket | None = None
        self._raw = False
        self._sequence = 0
        self._identifier = id(self) & 0xFFFF
        self._sent: dict[int, tuple[str, float]] = {}

    def open(self) -> bool:
        for kind, raw in ((socket.SOCK_DGRAM, False), (socket.SOCK_RAW, True)):
            try:
                self._sock = socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP)
                self._raw = raw
                self._sock.setblocking(False)
                return True
            except PermissionError:
                continue
        log.warning("Can't open an ICMP socket, so the link's round trip isn't measured")
        return False

    def _readable(self) -> None:
        while True:
            try:
                data = self._sock.recv(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            sequence = parse_reply(data)
            sent = self._sent.pop(sequence, None) if sequence is not None else None
            if sent is not None:
                address, at = sent
                self.on_result(address, (self.clock() - at) * 1e9)

    async def run(self) -> None:
        if self._sock is None and not self.open():
            return
        loop = asyncio.get_running_loop()
        loop.add_reader(self._sock.fileno(), self._readable)
        try:
            while True:
                now = self.clock()
                for sequence, (address, at) in list(self._sent.items()):
                    if now - at > TIMEOUT:
                        del self._sent[sequence]
                        self.on_result(address, None)
                for address in sorted(self.targets):
                    self._sequence = (self._sequence + 1) & 0xFFFF
                    packet = echo_request(self._identifier, self._sequence)
                    try:
                        self._sock.sendto(packet, (address, 0))
                        self._sent[self._sequence] = (address, self.clock())
                    except OSError:
                        self.on_result(address, None)
                await asyncio.sleep(INTERVAL)
        finally:
            loop.remove_reader(self._sock.fileno())
