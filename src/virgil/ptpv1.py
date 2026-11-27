"""Serving PTPv1 (IEEE 1588-2002) to Dante devices that only follow PTPv1.

Most Dante devices follow PTPv1, led on each side by a device that bridges
Virgil's PTPv2 into it, such as a console with PTPv2 on. A side without one
would free-run. So Virgil can lead PTPv1 itself, as the same clock it serves
over PTPv2: the NIC's hardware timestamps are PHC time, which is what ptp4l
serves, so the two agree exactly.

It announces PTP_EXT_SYNC, preferred and stratum 1, which Dante devices choose
over each other, and names this Virgil's MAC as grandmaster, which is how a
bridging Dante device already names it. It only asks the NIC for its hardware
timestamps; ptp4l has already set the NIC up to take them, and this never
changes that. Without them, it falls back to the kernel's software timestamps.

Messages, as Dante uses them: subdomain _DFLT, a Sync every 0.25 s with a
Follow_Up carrying when it really left, and a Delay_Resp to each Delay_Req.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import socket
import struct
import time

log = logging.getLogger("virgild.ptpv1")

GROUP = "224.0.1.129"
EVENT, GENERAL = 319, 320
SYNC, DELAY_REQ, FOLLOW_UP, DELAY_RESP = 0, 1, 2, 3
PTP_ASSIST, PTP_EXT_SYNC = 0x08, 0x10
SUBDOMAIN = b"_DFLT"
LOG_SYNC_INTERVAL = -2
IDENTIFIER = b"GPS"
# Past this many addresses that have sent a Delay_Req, the ones gone quiet are forgotten (see _answer_delay_requests).
MAX_FOLLOWERS = 256

HEADER = struct.Struct("!HH16sBB6sHHBBH4x")
# Offsets in the message: origin 40, grandmaster UUID 54, stratum 67, identifier 68, preferred 77,
# sync interval 83, steps removed 90, parent UUID 102 (124 bytes in all).
SYNC_BODY = struct.Struct("!IIhh x B 6s H H 3x B 4s 2x h x B x B 3x b 2x h 2x H 3x B 4s x B 6s 2x H 2x h i 3x B")
DELAY_RESP_BODY = struct.Struct("!II x B 6s H H")

SO_TIMESTAMPING, SCM_TIMESTAMPING, SO_BINDTODEVICE = 37, 37, 25
TX_HARDWARE, TX_SOFTWARE, RX_HARDWARE, RX_SOFTWARE, SOFTWARE, RAW_HARDWARE, OPT_TSONLY = (
    1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 6, 1 << 11)


def timestamping_flags(hardware_tx: bool) -> int:
    """What to ask the kernel for. Without hardware_tx, the card's transmit timestamp slots are left to ptp4l:
    sending is timed by the kernel, but packets the card receives (which don't use the slots) still get its time."""
    flags = TX_SOFTWARE | RX_HARDWARE | RX_SOFTWARE | SOFTWARE | RAW_HARDWARE | OPT_TSONLY
    return flags | TX_HARDWARE if hardware_tx else flags


def header(control: int, sequence: int, uuid: bytes, flags: int) -> bytes:
    message_type = 1 if control in (SYNC, DELAY_REQ) else 2
    return HEADER.pack(1, 1, SUBDOMAIN.ljust(16, b"\0"), message_type, 1, uuid, 1, sequence, control, 0, flags)


# Timestamps are (seconds, nanoseconds), exactly as the kernel gives them: a float near today's time can
# only resolve about 240 ns, which would throw away most of what hardware timestamps are for.
Stamp = tuple[int, int]


def now_stamp() -> Stamp:
    return divmod(time.time_ns(), 1_000_000_000)


def sync_message(t: Stamp, uuid: bytes, sequence: int) -> bytes:
    seconds, nanoseconds = t
    identifier = IDENTIFIER.ljust(4, b"\0")
    return header(SYNC, sequence, uuid, PTP_ASSIST | PTP_EXT_SYNC) + SYNC_BODY.pack(
        seconds, nanoseconds, 0, 0, 1, uuid, 1, sequence, 1, identifier, -16384, 1, 0, LOG_SYNC_INTERVAL,
        -16384, 0, 1, identifier, 1, uuid, 1, 0, 0, 0)


def follow_up_message(t: Stamp, uuid: bytes, sequence: int) -> bytes:
    seconds, nanoseconds = t
    return header(FOLLOW_UP, sequence, uuid, PTP_ASSIST | PTP_EXT_SYNC) + struct.pack("!2xHII", sequence, seconds,
                                                                                         nanoseconds)


def delay_resp_message(t: Stamp, uuid: bytes, requester: bytes, port: int, sequence: int) -> bytes:
    seconds, nanoseconds = t
    return header(DELAY_RESP, sequence, uuid, PTP_EXT_SYNC) + DELAY_RESP_BODY.pack(
        seconds, nanoseconds, 1, requester, port, sequence)


def parse_delay_req(data: bytes) -> tuple[bytes, int, int] | None:
    """A Delay_Req's sender (UUID and port) and sequence number, or None if it isn't one."""
    if len(data) < HEADER.size or data[:2] != b"\x00\x01" or data[32] != DELAY_REQ:
        return None
    return data[22:28], struct.unpack_from("!H", data, 28)[0], struct.unpack_from("!H", data, 30)[0]


def timestamp(data: bytes) -> tuple[Stamp | None, str]:
    """The best time in an SCM_TIMESTAMPING message: the NIC's (third) if there, else the kernel's (first)."""
    if len(data) >= 48:
        seconds, nanoseconds = struct.unpack_from("qq", data, 32)
        if seconds:
            return (seconds, nanoseconds), "hardware"
    if len(data) >= 16:
        seconds, nanoseconds = struct.unpack_from("qq", data, 0)
        if seconds:
            return (seconds, nanoseconds), "software"
    return None, "none"


class Server:
    """A PTPv1 grandmaster on one interface, run by virgild."""

    def __init__(self, interface: str, address: str, mac: str, hardware_tx: bool = True):
        self.interface, self.address = interface, address
        # Whether to ask the card for transmit timestamps: only when it can share them with ptp4l.
        self.hardware_tx = hardware_tx
        self.uuid = bytes.fromhex(mac.replace(":", ""))
        self.sequence = 0
        self.stats: collections.Counter = collections.Counter()
        self.followers: dict[str, float] = {}  # address: when it last sent a Delay_Req
        self._event: socket.socket | None = None
        self._general: socket.socket | None = None
        self._task: asyncio.Task | None = None

    def _socket(self, port: int, timestamps: bool) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # shared with ptp4l
        s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self.interface.encode())
        s.bind(("", port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(self.address))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.address))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        s.setblocking(False)
        if timestamps:
            s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, timestamping_flags(self.hardware_tx))
        return s

    def set_hardware_tx(self, hardware_tx: bool) -> None:
        """Change whether sending is timed by the card, on a running grandmaster, without a gap in its messages."""
        if hardware_tx == self.hardware_tx:
            return
        self.hardware_tx = hardware_tx
        if self._event is not None:
            self._event.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, timestamping_flags(hardware_tx))
        log.info("PTPv1 now takes its transmit timestamps from %s", "the NIC" if hardware_tx else "the kernel")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @staticmethod
    def _kind(hardware: int, software: int) -> str:
        return "hardware" if hardware and hardware >= software else "software" if software else "none yet"

    def timestamps(self) -> dict[str, str]:
        """Which timestamps it's using, for sending and for receiving: hardware, software, or none yet."""
        return {
            "send": self._kind(self.stats["hardware send times"], self.stats["software send times"]),
            "receive": self._kind(self.stats["hardware arrival times"],
                                  self.stats["software arrival times"] + self.stats["approximate arrival times"]),
        }

    def start(self) -> None:
        if self.running:
            return
        self._event = self._socket(EVENT, timestamps=True)
        self._general = self._socket(GENERAL, timestamps=False)
        asyncio.get_running_loop().add_reader(self._event.fileno(), self._answer_delay_requests)
        self._task = asyncio.create_task(self._run())
        log.info("Serving PTPv1 on %s, as %s", self.interface, self.uuid.hex(":"))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._event is not None:
            asyncio.get_running_loop().remove_reader(self._event.fileno())
            self._event.close()
            self._event = None
        if self._general is not None:
            self._general.close()
            self._general = None
        self.followers.clear()
        log.info("Stopped serving PTPv1")

    async def _sent_at(self) -> tuple[Stamp | None, str]:
        """When the last Sync really left: the NIC's time if it gives one within 20 ms, else the kernel's.

        Without hardware transmit timestamps, the kernel's is all that's coming, so there's no point waiting
        for more once it's here (every Follow-Up would be late by the wait).
        """
        best, kind = None, "none"
        for _ in range(40):
            try:
                _, ancillary, _, _ = self._event.recvmsg(1, 512, socket.MSG_ERRQUEUE)
            except BlockingIOError:
                if kind == "hardware" or (not self.hardware_tx and best is not None):
                    break
                await asyncio.sleep(0.0005)
                continue
            for level, kind_of, data in ancillary:
                if level == socket.SOL_SOCKET and kind_of == SCM_TIMESTAMPING:
                    stamp, source = timestamp(data)
                    if stamp is not None and (best is None or source == "hardware"):
                        best, kind = stamp, source
            if kind == "hardware" or (not self.hardware_tx and best is not None):
                break
        return best, kind

    async def _run(self) -> None:
        interval = 2.0 ** LOG_SYNC_INTERVAL
        next_sync = time.monotonic()
        while True:
            self.sequence = (self.sequence + 1) & 0xFFFF
            approximate = now_stamp()
            try:
                self._event.sendto(sync_message(approximate, self.uuid, self.sequence), (GROUP, EVENT))
                sent, kind = await self._sent_at()
                self.stats[f"{kind} send times"] += 1
                self._general.sendto(follow_up_message(sent if sent is not None else approximate, self.uuid,
                                                       self.sequence), (GROUP, GENERAL))
                self.stats["Syncs"] += 1
            except OSError as error:
                self.stats["send errors"] += 1
                if self.stats["send errors"] in (1, 100):
                    log.warning("Couldn't send PTPv1: %s", error)
            next_sync += interval
            await asyncio.sleep(max(0.0, next_sync - time.monotonic()))

    def _answer_delay_requests(self) -> None:
        while self._event is not None:
            try:
                data, ancillary, _, (source, _) = self._event.recvmsg(512, 512)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            request = parse_delay_req(data)
            if request is None:
                continue
            requester, port, sequence = request
            received, kind = now_stamp(), "approximate"
            for level, kind_of, raw in ancillary:
                if level == socket.SOL_SOCKET and kind_of == SCM_TIMESTAMPING:
                    stamp, source_kind = timestamp(raw)
                    if stamp is not None:
                        received, kind = stamp, source_kind
            self.stats[f"{kind} arrival times"] += 1
            try:
                self._general.sendto(delay_resp_message(received, self.uuid, requester, port, sequence),
                                     (GROUP, GENERAL))
                self.stats["Delay_Resps"] += 1
                now = time.monotonic()
                self.followers[source] = now
                if len(self.followers) > MAX_FOLLOWERS:
                    # Addresses were only ever added (until PTPv1 stopped), and anything on the network can send
                    # a Delay_Req from any address. Those not heard from lately are forgotten.
                    for address in [a for a, seen in self.followers.items() if now - seen > 60.0]:
                        del self.followers[address]
            except OSError:
                self.stats["send errors"] += 1

    def current_followers(self, within: float = 10.0) -> list[str]:
        now = time.monotonic()
        return sorted(a for a, seen in self.followers.items() if now - seen <= within)
