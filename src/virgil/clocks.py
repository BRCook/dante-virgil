"""Reading what chrony and phc2sys are doing, for status and heartbeats.

chrony answers monitoring commands from unprivileged users over localhost
(`chronyc -n tracking`). phc2sys has no query interface, so its latest
report is read from the journal, which needs the systemd-journal group.

Offsets are in nanoseconds, and frequencies in parts per million:

- system_offset_ns: the system clock's offset from what chrony follows, positive if ahead.
- frequency_ppm: how fast (positive) or slow the system clock runs on its own,
  as chrony has measured it. chrony corrects for it.
- phc_offset_ns: phc2sys's latest offset between the PHC and the system clock.
- phc_adjustment_ppm: the frequency adjustment phc2sys is applying to the PHC.
- phc_trim_ppm: on a leader, the rate the free-running PHC was set to.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

PHC2SYS_UNIT = "dante-virgil-phc2sys.service"

_REFERENCE = re.compile(r"^Reference ID\s*:\s*([0-9A-Fa-f]+)\s*\(([^)]*)\)", re.M)
_SYSTEM = re.compile(r"^System time\s*:\s*([0-9.]+) seconds (fast|slow)", re.M)
_SECONDS = r"^{label}\s*:\s*([+-]?[0-9.]+) seconds"
_FREQUENCY = re.compile(r"^Frequency\s*:\s*([0-9.]+) ppm (fast|slow)", re.M)
_SKEW = re.compile(r"^Skew\s*:\s*([0-9.]+) ppm", re.M)
_LEAP = re.compile(r"^Leap status\s*:\s*(.+)$", re.M)
_PHC2SYS = re.compile(r"sys offset\s+(-?\d+)\s+s(\d)\s+freq\s+([+-]?\d+)\s+delay\s+(\d+)")


def _ns(seconds: float) -> int:
    return round(seconds * 1e9)


def parse_tracking(text: str) -> dict[str, Any] | None:
    """Parse `chronyc -n tracking`. None if chrony isn't tracking anything."""
    reference = _REFERENCE.search(text)
    system = _SYSTEM.search(text)
    frequency = _FREQUENCY.search(text)
    if not (reference and system and frequency):
        return None
    result: dict[str, Any] = {
        "source": reference.group(2).strip() or reference.group(1),
        # chrony says the system clock is "fast" or "slow" of NTP time.
        "system_offset_ns": _ns(float(system.group(1))) * (1 if system.group(2) == "fast" else -1),
        "frequency_ppm": round(float(frequency.group(1)) * (1 if frequency.group(2) == "fast" else -1), 3),
    }
    for key, label in (("last_offset_ns", "Last offset"), ("rms_offset_ns", "RMS offset")):
        match = re.search(_SECONDS.format(label=label), text, re.M)
        if match:
            result[key] = _ns(float(match.group(1)))
    skew = _SKEW.search(text)
    if skew:
        result["skew_ppm"] = round(float(skew.group(1)), 3)
    leap = _LEAP.search(text)
    if leap:
        result["synchronised"] = leap.group(1).strip() != "Not synchronised"
    return result


def parse_phc2sys(text: str) -> dict[str, Any] | None:
    """The latest report in phc2sys's output: the PHC's offset from the system clock, and its adjustment."""
    matches = _PHC2SYS.findall(text)
    if not matches:
        return None
    offset, state, frequency, delay = matches[-1]
    return {
        "phc_offset_ns": int(offset),
        # s2 means locked; s0 and s1 are still settling.
        "phc_locked": state == "2",
        "phc_adjustment_ppm": round(int(frequency) / 1000, 3),
        "phc_read_delay_ns": int(delay),
    }


async def _output(*args: str, timeout: float = 3.0) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (FileNotFoundError, PermissionError):
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        return None
    return stdout.decode(errors="replace") if process.returncode == 0 else None


async def read_clock(follower: bool, trim_ppb: int | None) -> dict[str, Any]:
    """Everything worth knowing about this Virgil's clocks right now."""
    clock: dict[str, Any] = {}
    tracking = await _output("chronyc", "-n", "tracking")
    if tracking:
        clock.update(parse_tracking(tracking) or {})
    if follower:
        journal = await _output("journalctl", "-u", PHC2SYS_UNIT, "-n", "5", "-o", "cat", "--no-pager")
        if journal:
            clock.update(parse_phc2sys(journal) or {})
    elif trim_ppb is not None:
        clock["phc_trim_ppm"] = round(trim_ppb / 1000, 3)
    return clock
