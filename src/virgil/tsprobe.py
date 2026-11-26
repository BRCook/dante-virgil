"""Measuring a card's transmit timestamp slots, before ptp4l starts.

Run by `virgil-root sync` during a setup from scratch: ptp4l is stopped, and the
Dante devices are re-locking anyway, so nothing depends on the card for these few
seconds. The probe sends small frames that ask for hardware transmit timestamps,
and counts how many timestamps come back:

1. **Hardware timestamps at all:** one frame. No timestamp, no measurement.
2. **Slots:** several processes, one per CPU core (up to ten), each pinned to its
   own core, wait for the same instant on the system's monotonic clock, then each
   send one frame. That asks the card for that many timestamps at once, so the
   number that come back is the number of slots, up to the number of senders. The
   median of 20 such rounds is the count, so one round thrown off by the OS doesn't
   skew it. If every sender gets its timestamp, the card has *at least* that many.
3. **Contention:** two of those pinned processes, standing in for ptp4l and the
   PTPv1 grandmaster (separate programs, which can send at the same moment on
   different cores), each sending a frame at the same instant, 50 times. A card
   that can't share loses a timestamp from nearly every pair; one that can loses none.
4. **Load,** only if the contention test passed: two senders at 100 times their
   real rates (ptp4l's 8 Syncs a second and PTPv1's 4), independently, for
   1 second; then 50 times for 2 seconds; then 10 times for 5 seconds, stopping
   at the first that loses nothing. Losses at 100x that vanish at 50x are the
   card (or the CPU) falling behind on sheer volume, not the slots. The level that
   passes is the margin; a card that loses timestamps even at 10x can't share.

A timestamp that doesn't reach the probe isn't necessarily one the card dropped: on a
busy machine, a sender can give up waiting before it arrives. So where the driver
counts the timestamps it skipped or gave up on (in `ethtool -S`), the probe reads
those counters before and after each test, and only holds a loss against the card
if they rose. Drivers without such counters are judged on the losses alone.

The frames are plain Ethernet frames with the EtherType for local experiments
(0x88B5), sent to an unused, locally administered address. They aren't IP or PTP,
so Dante devices ignore them, and switches discard them.
"""

from __future__ import annotations

import fcntl
import os
import select
import socket
import statistics
import struct
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

ETHERTYPE = 0x88B5
DESTINATION = bytes.fromhex("02005e766972")  # locally administered, and unused
SO_TIMESTAMPING, SCM_TIMESTAMPING = 37, 37
TX_HARDWARE, RAW_HARDWARE, OPT_TSONLY = 1 << 0, 1 << 6, 1 << 11

MAX_SENDERS = 10
# How long a sender waits for its timestamp: generous, since a busy machine at boot can be slow to hand it over.
TIMESTAMP_WAIT = 0.2
SLOT_ROUNDS = 20
PAIRS = 50
LOADS = ((100, 1.0), (50, 2.0), (10, 5.0))  # (times the real rates, seconds)
REAL_RATES = (8.0, 4.0)  # ptp4l's Syncs a second, and PTPv1's


@dataclass
class ProbeResult:
    hardware: bool  # whether the card gave hardware transmit timestamps at all
    slots: int | None = None  # how many timestamps it held at once (the median of the rounds)
    senders: int | None = None  # how many senders asked at once: slots equal to this means "at least this many"
    pairs_lost: int | None = None  # timestamps lost from simultaneous pairs, out of 2 x PAIRS
    pairs_dropped: int | None = None  # how many of those the driver itself counted as skipped or timed out
    margin: int | None = None  # the highest multiple of the real rates that lost nothing
    losses: dict[str, str] = field(default_factory=dict)  # each load level: "lost/sent"
    seconds: float = 0.0
    when: float = 0.0

    @property
    def contention_passed(self) -> bool:
        """Nothing lost, or nothing the card itself dropped: a loss the driver didn't count was the probe's."""
        return self.pairs_lost == 0 or self.pairs_dropped == 0

    @property
    def shareable(self) -> bool:
        return self.hardware and (self.slots or 0) >= 2 and self.contention_passed and self.margin is not None

    def as_dict(self) -> dict:
        return {**asdict(self), "shareable": self.shareable}


class Card:
    """The two senders the probe needs. Real ones use raw sockets; tests use a simulated card."""

    def send(self, which: int) -> None:
        raise NotImplementedError

    def collect(self, which: int, wait: float) -> int:
        """How many hardware transmit timestamps have come back on that sender, waiting up to `wait` seconds."""
        raise NotImplementedError

    @property
    def parallel(self) -> int:
        """How many senders can genuinely send at the same instant."""
        raise NotImplementedError

    def simultaneous(self, senders: int) -> int:
        """That many senders each send one frame at the same instant: how many get their timestamps."""
        raise NotImplementedError

    def dropped(self) -> int | None:
        """The driver's own count of transmit timestamps it skipped or gave up on, or None if it doesn't count."""
        return None

    def close(self) -> None:
        pass


def probe(card: Card, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> ProbeResult:
    started = clock()
    card.send(0)
    if card.collect(0, 0.2) == 0:
        return ProbeResult(hardware=False, seconds=round(clock() - started, 2), when=time.time())

    senders = min(MAX_SENDERS, card.parallel)
    slots = round(statistics.median(card.simultaneous(senders) for _ in range(SLOT_ROUNDS)))

    before = card.dropped()
    lost = 0
    for _ in range(PAIRS):
        lost += 2 - card.simultaneous(2)
        sleep(0.02)
    after = card.dropped()
    dropped = None if before is None or after is None else after - before

    result = ProbeResult(hardware=True, slots=slots, senders=senders, pairs_lost=lost, pairs_dropped=dropped)
    if result.contention_passed:
        for multiple, seconds in LOADS:
            before = card.dropped()
            sent, got = _load(card, multiple, seconds, clock, sleep)
            after = card.dropped()
            dropped = None if before is None or after is None else after - before
            result.losses[f"{multiple}x"] = f"{sent - got}/{sent}" + ("" if dropped is None else f" ({dropped} by the NIC)")
            if got == sent or dropped == 0:
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


def _socket(interface: str) -> socket.socket:
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    sock.bind((interface, 0))
    sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, TX_HARDWARE | RAW_HARDWARE | OPT_TSONLY)
    sock.setblocking(False)
    return sock


def _timestamps(sock: socket.socket, wait: float) -> int:
    """How many hardware transmit timestamps come back on a socket within `wait` seconds."""
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


class _Senders:
    """Sender processes, each pinned to its own CPU core, that send one frame each at an agreed instant."""

    def __init__(self, interface: str, frame: bytes, cores: list[int], count: int):
        self.workers = []  # (pid, the parent's writing end, the parent's reading end)
        for index in range(count):
            core = cores[index % len(cores)]  # round-robin when there are more senders than cores
            child_reads, parent_writes = os.pipe()  # os.pipe() gives (reading end, writing end)
            parent_reads, child_writes = os.pipe()
            pid = os.fork()
            if pid == 0:  # the sender
                try:
                    os.close(parent_writes)
                    os.close(parent_reads)
                    os.sched_setaffinity(0, {core})
                    sock = _socket(interface)
                    while True:
                        message = os.read(child_reads, 8)
                        deadline = struct.unpack("!q", message)[0] if len(message) == 8 else 0
                        if deadline == 0:
                            break
                        while time.monotonic_ns() < deadline:
                            pass  # spin, so the send happens at the instant, not whenever we're woken
                        try:
                            sock.send(frame)
                        except OSError:
                            pass
                        os.write(child_writes, b"1" if _timestamps(sock, TIMESTAMP_WAIT) else b"0")
                finally:
                    os._exit(0)
            os.close(child_reads)
            os.close(child_writes)
            self.workers.append((pid, parent_writes, parent_reads))

    def fire(self, senders: int, lead: float = 0.01) -> int:
        deadline = time.monotonic_ns() + int(lead * 1e9)
        chosen = self.workers[:senders]
        for _, write, _ in chosen:
            os.write(write, struct.pack("!q", deadline))
        return sum(os.read(read, 1) == b"1" for _, _, read in chosen)

    def close(self) -> None:
        for pid, write, read in self.workers:
            try:
                os.write(write, struct.pack("!q", 0))
            except OSError:
                pass
            os.close(write)
            os.close(read)
            os.waitpid(pid, 0)
        self.workers = []


class RawCard(Card):
    """Raw sockets on a real interface, each asking for hardware transmit timestamps."""

    def __init__(self, interface: str):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            source = fcntl.ioctl(s.fileno(), 0x8927, struct.pack("256s", interface.encode()[:15]))[18:24]
        self.frame = DESTINATION + source + struct.pack("!H", ETHERTYPE) + b"virgil timestamp probe".ljust(46, b"\0")
        self.interface = interface
        self.sockets = [_socket(interface) for _ in range(2)]
        self.cores = sorted(os.sched_getaffinity(0))[:MAX_SENDERS]
        self._senders: _Senders | None = None

    @property
    def parallel(self) -> int:
        return len(self.cores)

    def simultaneous(self, senders: int) -> int:
        if self._senders is None:
            # At least two, for the contention test: on one core, back-to-back sends can still find a slot full.
            self._senders = _Senders(self.interface, self.frame, self.cores, max(2, len(self.cores)))
        return self._senders.fire(senders)

    def send(self, which: int) -> None:
        try:
            self.sockets[which].send(self.frame)
        except OSError:
            pass  # a frame the kernel wouldn't take is simply a frame without a timestamp

    def collect(self, which: int, wait: float) -> int:
        return _timestamps(self.sockets[which], wait)

    def dropped(self) -> int | None:
        import subprocess

        from virgil.timestamping import skipped_counters

        try:
            stats = subprocess.run(["ethtool", "-S", self.interface], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        counters = skipped_counters(stats)
        return sum(counters.values()) if counters else None

    def close(self) -> None:
        if self._senders is not None:
            self._senders.close()
        for sock in self.sockets:
            sock.close()
