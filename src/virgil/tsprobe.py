"""Measuring a card's transmit timestamp slots, before ptp4l starts.

Run by `virgil-root sync` during a setup from scratch: ptp4l is stopped, and the
Dante devices are re-locking anyway, so nothing depends on the card for these few
seconds. The probe sends small frames that ask for hardware transmit timestamps,
and counts how many timestamps come back:

1. **Hardware timestamps at all:** one frame. No timestamp, no measurement.
2. **Slots:** bursts of 64 frames back to back, five times. A card can only hold
   as many timestamps at once as it has slots, so the most that come back from
   one burst is the number of slots.
3. **Contention:** two senders, standing in for ptp4l and the PTPv1 grandmaster,
   each sending a frame at the same moment, 50 times. That's sharing's worst case,
   made certain rather than left to chance. A card that can't share loses a
   timestamp from nearly every pair; one that can loses none.
4. **Load,** only if the contention test passed: the two senders at 100 times
   their real rates (ptp4l's 8 Syncs a second and PTPv1's 4), independently, for
   1 second; then 50 times for 2 seconds; then 10 times for 5 seconds, stopping
   at the first that loses nothing. Losses at 100x that vanish at 50x are the
   card (or the CPU) falling behind on sheer volume, not the slots. The level that
   passes is the margin; a card that loses timestamps even at 10x can't share.

The frames are plain Ethernet frames with the EtherType for local experiments
(0x88B5), sent to an unused, locally administered address. They aren't IP or PTP,
so Dante devices ignore them, and switches discard them.
"""

from __future__ import annotations

import fcntl
import select
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

ETHERTYPE = 0x88B5
DESTINATION = bytes.fromhex("02005e766972")  # locally administered, and unused
SO_TIMESTAMPING, SCM_TIMESTAMPING = 37, 37
TX_HARDWARE, RAW_HARDWARE, OPT_TSONLY = 1 << 0, 1 << 6, 1 << 11

BURST, BURSTS = 64, 5
PAIRS = 50
LOADS = ((100, 1.0), (50, 2.0), (10, 5.0))  # (times the real rates, seconds)
REAL_RATES = (8.0, 4.0)  # ptp4l's Syncs a second, and PTPv1's


@dataclass
class ProbeResult:
    hardware: bool  # whether the card gave hardware transmit timestamps at all
    slots: int | None = None  # the most timestamps it held at once
    pairs_lost: int | None = None  # timestamps lost from simultaneous pairs, out of PAIRS
    margin: int | None = None  # the highest multiple of the real rates that lost nothing
    losses: dict[str, str] = field(default_factory=dict)  # each load level: "lost/sent"
    seconds: float = 0.0
    when: float = 0.0

    @property
    def shareable(self) -> bool:
        return (self.hardware and (self.slots or 0) >= 2 and self.pairs_lost == 0 and self.margin is not None)

    def as_dict(self) -> dict:
        return {**asdict(self), "shareable": self.shareable}


class Card:
    """The two senders the probe needs. Real ones use raw sockets; tests use a simulated card."""

    def send(self, which: int) -> None:
        raise NotImplementedError

    def collect(self, which: int, wait: float) -> int:
        """How many hardware transmit timestamps have come back on that sender, waiting up to `wait` seconds."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def probe(card: Card, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> ProbeResult:
    started = clock()
    card.send(0)
    if card.collect(0, 0.2) == 0:
        return ProbeResult(hardware=False, seconds=round(clock() - started, 2), when=time.time())

    slots = 0
    for _ in range(BURSTS):
        for _ in range(BURST):
            card.send(0)
        slots = max(slots, card.collect(0, 0.2))
        sleep(0.05)

    lost = 0
    for _ in range(PAIRS):
        card.send(0)
        card.send(1)
        lost += 2 - card.collect(0, 0.02) - card.collect(1, 0.02)
        sleep(0.02)

    result = ProbeResult(hardware=True, slots=slots, pairs_lost=lost)
    if lost == 0:
        for multiple, seconds in LOADS:
            sent, got = _load(card, multiple, seconds, clock, sleep)
            result.losses[f"{multiple}x"] = f"{sent - got}/{sent}"
            if got == sent:
                result.margin = multiple
                break
    result.seconds = round(clock() - started, 2)
    result.when = time.time()
    return result


def _load(card: Card, multiple: int, seconds: float, clock, sleep) -> tuple[int, int]:
    """Both senders at `multiple` times their real rates, independently, for `seconds`."""
    intervals = [1.0 / (rate * multiple) for rate in REAL_RATES]
    start = clock()
    due = [start, start + intervals[1] / 3]  # out of step, as two independent programs would be
    sent = got = 0
    while clock() - start < seconds:
        now = clock()
        for which in (0, 1):
            if now >= due[which]:
                card.send(which)
                sent += 1
                due[which] += intervals[which]
        got += card.collect(0, 0) + card.collect(1, 0)
        sleep(max(0.0, min(due) - clock()))
    return sent, got + card.collect(0, 0.2) + card.collect(1, 0.2)


class RawCard(Card):
    """Two raw sockets on a real interface, each asking for hardware transmit timestamps."""

    def __init__(self, interface: str):
        self.sockets = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            source = fcntl.ioctl(s.fileno(), 0x8927, struct.pack("256s", interface.encode()[:15]))[18:24]
        self.frame = DESTINATION + source + struct.pack("!H", ETHERTYPE) + b"virgil timestamp probe".ljust(46, b"\0")
        for _ in range(2):
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
            sock.bind((interface, 0))
            sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, TX_HARDWARE | RAW_HARDWARE | OPT_TSONLY)
            sock.setblocking(False)
            self.sockets.append(sock)

    def send(self, which: int) -> None:
        try:
            self.sockets[which].send(self.frame)
        except OSError:
            pass  # a frame the kernel wouldn't take is simply a frame without a timestamp

    def collect(self, which: int, wait: float) -> int:
        sock = self.sockets[which]
        count, deadline = 0, time.monotonic() + wait
        while True:
            try:
                _, ancillary, _, _ = sock.recvmsg(1, 1024, socket.MSG_ERRQUEUE)
            except (BlockingIOError, InterruptedError):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return count
                select.select([], [], [sock], min(remaining, 0.005))
                continue
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == SCM_TIMESTAMPING and len(data) >= 48:
                    seconds, nanoseconds = struct.unpack_from("qq", data, 32)
                    if seconds or nanoseconds:
                        count += 1

    def close(self) -> None:
        for sock in self.sockets:
            sock.close()
