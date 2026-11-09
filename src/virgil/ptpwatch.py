"""Watching PTP on the PTP interface.

A raw socket with a kernel filter sees every PTP packet on the PTP interface
(UDP ports 319 and 320), and nothing else, so audio never reaches Virgil. From
them it learns:

- this Virgil's own grandmaster identity, from the Announce messages ptp4l sends;
- its local Dante devices: every Dante device sends PTPv1 for Dante's own
  clocking, and PTPv2 Delay Requests once it follows this Virgil. PTP never
  crosses the radio, so anything sending PTP here is local;
- leaks: an Announce from another member's grandmaster means PTP is crossing
  the radio, and one from any other grandmaster is a foreign leader.

Listening needs CAP_NET_RAW.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("virgild.ptp")

ETH_P_IP = 0x0800
# Linux only gives outgoing packets to raw sockets opened for every protocol,
# and this Virgil's own Announce messages are outgoing. The kernel filter
# still drops everything that isn't PTP over IPv4.
ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4
SO_ATTACH_FILTER = 26
SNAPLEN = 256

# PTPv2 message types.
SYNC, DELAY_REQ, FOLLOW_UP, DELAY_RESP, ANNOUNCE = 0x0, 0x1, 0x8, 0x9, 0xB

# How long a device or a foreign grandmaster is remembered after its last packet.
FORGET_AFTER = 30.0


# A classic BPF program: IPv4, UDP, not a later fragment, destination port 319 or 320.
def _filter() -> list[tuple[int, int, int, int]]:
    return [
        (0x28, 0, 0, 12),           # ldh [12]            ethertype
        (0x15, 0, 9, ETH_P_IP),     # jeq #0x800          else drop
        (0x30, 0, 0, 23),           # ldb [23]            IP protocol
        (0x15, 0, 7, 17),           # jeq #17 (UDP)       else drop
        (0x28, 0, 0, 20),           # ldh [20]            flags and fragment offset
        (0x45, 5, 0, 0x1FFF),       # jset #0x1fff        fragment: drop
        (0xB1, 0, 0, 14),           # ldxb 4*([14]&0xf)   IP header length
        (0x48, 0, 0, 16),           # ldh [x+16]          UDP destination port
        (0x15, 1, 0, 319),          # jeq #319            accept
        (0x15, 0, 1, 320),          # jeq #320            accept, else drop
        (0x06, 0, 0, SNAPLEN),      # ret #SNAPLEN        accept
        (0x06, 0, 0, 0),            # ret #0              drop
    ]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def attach_filter(sock: socket.socket) -> None:
    program = _filter()
    instructions = (_SockFilter * len(program))(*[_SockFilter(*i) for i in program])
    fprog = _SockFprog(len(program), instructions)
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, bytes(fprog))


@dataclass(frozen=True)
class PtpPacket:
    source: str
    outgoing: bool
    version: int
    message_type: int | None  # PTPv2 only
    clock_identity: str | None  # PTPv2 sender's clock identity
    grandmaster: str | None  # PTPv2 Announce only


def _identity(raw: bytes) -> str:
    return raw.hex(":")


def parse(frame: bytes, outgoing: bool = False) -> PtpPacket | None:
    """Parse an Ethernet frame carrying PTP over UDP/IPv4, or None if it isn't one."""
    if len(frame) < 14 + 20 + 8 + 2 or struct.unpack_from("!H", frame, 12)[0] != ETH_P_IP:
        return None
    ihl = (frame[14] & 0x0F) * 4
    if ihl < 20 or frame[23] != 17:
        return None
    source = socket.inet_ntoa(frame[26:30])
    payload = frame[14 + ihl + 8:]
    if len(payload) < 2:
        return None
    if payload[0] == 0x00 and payload[1] == 0x01:
        return PtpPacket(source, outgoing, 1, None, None, None)
    if payload[1] & 0x0F != 2 or len(payload) < 34:
        return None
    message_type = payload[0] & 0x0F
    clock_identity = _identity(payload[20:28])
    grandmaster = _identity(payload[53:61]) if message_type == ANNOUNCE and len(payload) >= 61 else None
    return PtpPacket(source, outgoing, 2, message_type, clock_identity, grandmaster)


@dataclass
class LocalDevice:
    address: str
    last_seen: float
    ptpv1: bool = False
    # Sending Delay Requests to this Virgil's grandmaster means it's following it.
    follows_this_virgil: bool = False
    clock_identity: str | None = None


@dataclass
class PtpState:
    """What PTP watching has learned, kept up to date by observe()."""

    own_identity: str | None = None
    # When PTP was last heard from anything other than this Virgil.
    last_heard: float | None = None
    devices: dict[str, LocalDevice] = field(default_factory=dict)
    # Grandmasters other than this Virgil's, by identity, with when each was last heard.
    foreign: dict[str, tuple[str, float]] = field(default_factory=dict)

    def observe(self, packet: PtpPacket, now: float, own_addresses: set[str]) -> None:
        if packet.outgoing or packet.source in own_addresses:
            if packet.message_type == ANNOUNCE and packet.grandmaster:
                if self.own_identity != packet.grandmaster:
                    log.info("This Virgil's grandmaster identity is %s", packet.grandmaster)
                self.own_identity = packet.grandmaster
            return
        self.last_heard = now
        if packet.message_type == ANNOUNCE and packet.grandmaster:
            if packet.grandmaster != self.own_identity:
                if packet.grandmaster not in self.foreign:
                    log.warning("Heard another grandmaster, %s, from %s", packet.grandmaster, packet.source)
                self.foreign[packet.grandmaster] = (packet.source, now)
            return
        device = self.devices.get(packet.source)
        if device is None:
            device = self.devices[packet.source] = LocalDevice(packet.source, now)
            log.info("Found a local Dante device at %s", packet.source)
        device.last_seen = now
        if packet.version == 1:
            device.ptpv1 = True
        elif packet.message_type == DELAY_REQ:
            device.follows_this_virgil = True
            device.clock_identity = packet.clock_identity

    def forget_old(self, now: float) -> None:
        for address in [a for a, d in self.devices.items() if now - d.last_seen > FORGET_AFTER]:
            log.info("Lost the local Dante device at %s", address)
            del self.devices[address]
        for identity in [i for i, (_, seen) in self.foreign.items() if now - seen > FORGET_AFTER]:
            del self.foreign[identity]

    def leaks(self, member_identities: dict[str, str], explained: set[str] = frozenset()) -> list[str]:
        """Warnings about grandmasters heard on the PTP interface.

        member_identities maps identity to name. explained holds the addresses
        of local devices whose own warning already says why they're announcing.
        """
        warnings = []
        for identity, (source, _) in sorted(self.foreign.items()):
            if source in explained:
                continue
            member = member_identities.get(identity)
            if member is not None:
                warnings.append(f"PTP from {member} is crossing the link (heard from {source})")
            else:
                warnings.append(f"Another PTP grandmaster is on this network ({identity}, from {source})")
        return warnings


class PtpWatcher:
    """Reads PTP packets from the PTP interface and feeds them to a PtpState."""

    def __init__(self, interface: str, state: PtpState, clock: Callable[[], float],
                 own_addresses: Callable[[], set[str]]):
        self.interface = interface
        self.state = state
        self._clock = clock
        self._own_addresses = own_addresses
        self._sock: socket.socket | None = None

    def start(self, loop) -> None:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        attach_filter(sock)
        sock.bind((self.interface, ETH_P_ALL))
        sock.setblocking(False)
        self._sock = sock
        loop.add_reader(sock.fileno(), self._readable)
        log.info("Watching PTP on %s", self.interface)

    def _readable(self) -> None:
        assert self._sock is not None
        while True:
            try:
                frame, address = self._sock.recvfrom(SNAPLEN)
            except BlockingIOError:
                return
            except OSError as error:
                log.warning("PTP watching stopped: %s", error)
                return
            packet = parse(frame, outgoing=address[2] == PACKET_OUTGOING)
            if packet is not None:
                self.state.observe(packet, self._clock(), self._own_addresses())

    def stop(self, loop) -> None:
        if self._sock is not None:
            loop.remove_reader(self._sock.fileno())
            self._sock.close()
            self._sock = None
