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
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_PATH = Path("/var/lib/dante-virgil/timestamping.json")
# Written by virgil-root (as root), so kept apart from the evidence virgild writes.
PROBE_PATH = Path("/var/lib/dante-virgil/timestamping-probe.json")

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
            if self.shareable:
                return f"{self.count} transmit timestamp slots (probed; shared safely at {self.margin}× normal load)"
            return f"{self.count} transmit timestamp slots (probed), but sharing lost timestamps under test"
        noun = "slot" if self.count == 1 else "slots"
        why = {"evidence": "seen losing timestamps", "probe": "probed"}.get(self.source, self.source)
        return f"{self.count} transmit timestamp {noun} ({why})"

    def as_dict(self) -> dict:
        return {"count": self.count, "source": self.source, "can_share": self.can_share, "margin": self.margin,
                "description": self.describe()}


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


def save_probe(key: str, result: dict, path: Path = PROBE_PATH) -> None:
    data = load_probes(path)
    data[key] = result
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.chmod(0o644)  # virgild reads it as the dante-virgil user
    temporary.replace(path)


def slots_for(interface: str, driver: str | None, release: str | None = None,
              path: Path = EVIDENCE_PATH, probe_path: Path = PROBE_PATH) -> Slots:
    """What Virgil has measured about a card's slots: evidence first, then the probe, else unknown."""
    key = card_key(interface, driver, release)
    if key in load_evidence(path):
        return Slots(1, "evidence")
    probed = load_probes(probe_path).get(key)
    if isinstance(probed, dict):
        if not probed.get("hardware"):
            return Slots(0, "probe", shareable=False)
        return Slots(probed.get("slots"), "probe", shareable=bool(probed.get("shareable")), margin=probed.get("margin"))
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
