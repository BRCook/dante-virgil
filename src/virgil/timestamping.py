"""How many transmit timestamps a network card can hold at once, and how Virgil knows.

When a program asks for an outgoing packet to be timestamped, the card keeps the
timestamp in a transmit timestamp slot until the driver collects it. A request
made while every slot is full is skipped. ptp4l and Virgil's PTPv1 grandmaster
both ask for transmit timestamps, so on a card with one slot they can push each
other out; if it's ptp4l that loses, it gives up ("timed out while polling for tx
timestamp") and resets its port, and Dante devices see their grandmaster vanish.

Linux has no standard way to ask a card how many slots it has, and Virgil doesn't
guess from a list of drivers: a count is only believed once it has been measured.
Until then, a card's slots are unknown, and it's treated as having one, since
assuming more could knock ptp4l over. The measurements:

- a probe during a setup from scratch, before ptp4l starts (see virgil.tsprobe);
- evidence: a card caught skipping transmit timestamps while ptp4l and PTPv1
  shared it is treated as having one slot from then on, even after a reboot.
  Evidence beats the probe.
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from virgil.files import atomic_write

EVIDENCE_PATH = Path("/var/lib/dante-virgil/timestamping.json")
# Written by virgil-root, as root, so it lives in a directory only root can write (see save_probe). It used to sit
# beside the evidence, in /var/lib/dante-virgil, which belongs to the dante-virgil user: root writing there could
# be steered, with a symlink, into overwriting any file on the machine.
PROBE_PATH = Path("/var/lib/dante-virgil-root/timestamping-probe.json")
# Where the probe results were kept up to Virgil 1.1.0. Only ever read, and only by virgild (see slots_for), so
# an updated Clock Box still knows its card's slots until the next setup from scratch probes it again.
LEGACY_PROBE_PATH = Path("/var/lib/dante-virgil/timestamping-probe.json")

# ptp4l's message when its transmit timestamp never arrives.
PTP4L_TIMEOUT = "timed out while polling for tx timestamp"

# Driver statistics (ethtool -S) counting transmit timestamps that were skipped or lost.
SKIPPED_COUNTER = re.compile(r"^\s*([\w.]*hwtstamp[\w.]*(?:skip|timeout|lost|drop|err)[\w.]*):\s*(\d+)\s*$",
                             re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Slots:
    count: int | None  # None: unknown
    source: str  # "probe", "evidence" or "unknown"
    # From a probe: whether sharing passed the contention and load tests, and at what multiple of the real rates.
    shareable: bool | None = None
    margin: int | None = None
    # How many senders the probe used: a count equal to this means "at least this many".
    senders: int | None = None

    @property
    def at_least(self) -> bool:
        return self.senders is not None and self.count is not None and self.count >= self.senders

    @property
    def count_text(self) -> str:
        """The count as shown: "4", "10+" (at least ten), or "unknown"."""
        if self.count is None:
            return "unknown"
        return f"{self.count}+" if self.at_least else str(self.count)

    @property
    def effective(self) -> int:
        """How many slots to plan around: unknown counts as one."""
        return 1 if self.count is None else self.count

    @property
    def can_share(self) -> bool:
        """Whether ptp4l and the PTPv1 grandmaster can both use the card's transmit timestamps."""
        if self.shareable is not None:
            return self.shareable
        return self.effective >= 2

    def describe(self) -> str:
        if self.count is None:
            return "Transmit timestamp slots unknown, so treated as 1"
        if self.count == 0:
            return "No hardware transmit timestamps (probed)"
        if self.source == "probe" and self.count >= 2 and self.shareable is not None:
            count = f"At least {self.count}" if self.at_least else str(self.count)
            if self.shareable:
                return f"{count} transmit timestamp slots (probed; shared safely at {self.margin}× normal load)"
            return f"{count} transmit timestamp slots (probed), but sharing lost timestamps under test"
        noun = "slot" if self.count == 1 else "slots"
        why = {"evidence": "seen losing timestamps", "probe": "probed"}.get(self.source, self.source)
        return f"{self.count} transmit timestamp {noun} ({why})"

    def short(self) -> str:
        """A label that fits beside an interface; describe() gives the whole story."""
        if self.count is None:
            return "TX slots unknown"
        if self.count == 0:
            return "No hardware TX timestamps"
        noun = "TX slot" if self.count == 1 and not self.at_least else "TX slots"
        if self.source == "evidence":
            return f"{self.count_text} {noun}, lost timestamps"
        if self.shareable is not None:
            return f"{self.count_text} {noun}, {'shares safely' if self.shareable else 'can’t share'}"
        return f"{self.count_text} {noun}"

    def reason(self) -> str:
        """Why PTPv1 can't use the card's transmit timestamps, to follow the interface's name."""
        if self.count is None:
            return "hasn't had its transmit timestamp slots measured yet"
        if self.count == 0:
            return "gives no hardware transmit timestamps"
        if self.source == "evidence":
            return "lost transmit timestamps while ptp4l and PTPv1 shared them"
        if self.count == 1:
            return "has 1 transmit timestamp slot"
        return "couldn't share its transmit timestamps under test"

    def as_dict(self) -> dict:
        return {"count": self.count, "count_text": self.count_text, "source": self.source, "can_share": self.can_share,
                "margin": self.margin, "description": self.describe(), "short": self.short(), "reason": self.reason()}


def card_key(interface: str, driver: str | None, release: str | None = None) -> str:
    """Evidence belongs to one card, on one driver, on one kernel: an update may change things."""
    return f"{interface}|{driver or 'unknown'}|{release or platform.release()}"


def load_evidence(path: Path = EVIDENCE_PATH) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_evidence(key: str, reason: str, path: Path = EVIDENCE_PATH) -> None:
    data = load_evidence(path)
    data[key] = {"slots": 1, "reason": reason, "seen": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def load_probes(path: Path = PROBE_PATH) -> dict:
    return load_evidence(path)


def _only_root_writes(folder: Path) -> None:
    """Refuse, when running as root, to write in a directory anyone but root could have prepared.

    Root must never create or replace a file in a directory an unprivileged user owns: that user can leave a
    symlink where root is about to write, and root's write then lands wherever the symlink points. So the
    directory has to be a real directory (not a symlink to one), owned by root, and writable by nobody else.
    Run as anyone else (virgild never writes probes; the tests do), there's no privilege to borrow, so no check.
    """
    if os.geteuid() != 0:
        return
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise PermissionError(f"{folder} isn't a directory that only root can write, so nothing is written there")


def save_probe(key: str, result: dict, path: Path = PROBE_PATH) -> None:
    """Record a card's probe. Run by virgil-root, as root; raises OSError if it can't be written safely."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _only_root_writes(path.parent)
    data = load_probes(path)
    data[key] = result
    # atomic_write makes its temporary file with mkstemp, which never follows a symlink or reuses a file that's
    # already there (the old code wrote to a fixed name, "timestamping-probe.new", and followed whatever was
    # there). 0644: virgild reads it as the dante-virgil user.
    atomic_write(path, json.dumps(data, indent=2).encode(), mode=0o644)


def slots_for(interface: str, driver: str | None, release: str | None = None,
              path: Path = EVIDENCE_PATH, probe_path: Path = PROBE_PATH) -> Slots:
    """What Virgil has measured about a card's slots: evidence first, then the probe, else unknown."""
    key = card_key(interface, driver, release)
    if key in load_evidence(path):
        return Slots(1, "evidence")
    probed = load_probes(probe_path).get(key)
    if probed is None and probe_path == PROBE_PATH:
        # Probed by Virgil 1.1.0 or earlier, and not again since. virgild can write this old file itself, so it's
        # only used here, for what virgild believes about its own card; virgil-root never reads it.
        probed = load_probes(LEGACY_PROBE_PATH).get(key)
    if isinstance(probed, dict):
        if not probed.get("hardware"):
            return Slots(0, "probe", shareable=False)
        return Slots(probed.get("slots"), "probe", shareable=bool(probed.get("shareable")), margin=probed.get("margin"),
                     senders=probed.get("senders"))
    return Slots(None, "unknown")


def skipped_counters(ethtool_stats: str) -> dict[str, int]:
    """The driver's counters of skipped or lost transmit timestamps, from `ethtool -S`."""
    return {name: int(value) for name, value in SKIPPED_COUNTER.findall(ethtool_stats)}


class Watch:
    """Notices transmit timestamps going missing: a counter rising, or ptp4l timing out."""

    def __init__(self) -> None:
        self.baseline: dict[str, int] | None = None

    def counters_rose(self, counters: dict[str, int]) -> str | None:
        if self.baseline is None:
            self.baseline = counters
            return None
        risen = [name for name, value in counters.items() if value > self.baseline.get(name, value)]
        self.baseline = counters
        return f"{risen[0]} rose" if risen else None

    @staticmethod
    def ptp4l_timed_out(journal: str) -> str | None:
        return "ptp4l timed out waiting for a transmit timestamp" if PTP4L_TIMEOUT in journal else None
