"""Automatic trim: nudging the leader's PHC rate toward the consoles' own.

Each console reports how far it has to pull its own clock to follow PTP
(netaudio's clock_frequency_offset_parts_per_billion). Raising the leader's
PHC rate raises that figure, so a positive average means the leader is
running faster than the consoles would like, and the trim has to come down.

The size of the response isn't known exactly, so each step only corrects a
fraction of the average, and is capped. That converges for any response up
to several times the trim change, and settles within a few minutes.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable

# Gentle on purpose: every step changes the rate the followers' chrony is
# measuring, and a big or frequent change leaves the followers behind.
INTERVAL = 60.0
# Correct this fraction of the consoles' average offset each step.
GAIN = 0.3
# Leave the trim alone within this band, in ppb.
DEADBAND = 200
# The largest change in one step, in ppb.
MAX_STEP = 100
# Further out than FAST_BEYOND, steps may be up to FAST_STEP, so a cold start's trim settles in a quarter of an hour
# or so rather than most of one. The final approach is still at MAX_STEP. Followers kept up with steady 0.1 ppm
# steps at an RMS of 3 to 4 µs; auto-trim still waits for them to settle after every step, so if they can't keep up
# with bigger ones, it simply slows down.
FAST_BEYOND = 1000
FAST_STEP = 300
# The trim never goes beyond this, in ppb.
MAX_TRIM = 100_000
# Readings beyond this, in ppb, can't come from a locked console.
PLAUSIBLE = 100_000


def plausible(offsets: Iterable[int]) -> list[int]:
    return [o for o in offsets if abs(o) <= PLAUSIBLE]


def next_trim(trim_ppb: int, offsets_ppb: Iterable[int]) -> int | None:
    """The trim to change to, or None to leave it as it is."""
    readings = plausible(offsets_ppb)
    if not readings:
        return None
    average = sum(readings) / len(readings)
    if abs(average) <= DEADBAND:
        return None
    limit = FAST_STEP if abs(average) > FAST_BEYOND else MAX_STEP
    step = max(-limit, min(limit, round(-GAIN * average)))
    new = max(-MAX_TRIM, min(MAX_TRIM, trim_ppb + step))
    return None if new == trim_ppb else new


# Each step steers by each Dante device's average reading over the last five minutes, not its reading at that
# instant. A device behind a follower wanders by a ppm or so over minutes, as the follower tracks the leader
# across a jittery link; steering by single readings, the trim chased that wander up and down (and each step
# added to it). Averaging over a full five minutes, it only moves for an offset that lasts.
#
# A device's reading moves with the trim (lowering the trim by 0.1 ppm lowers the readings by about 0.1 ppm), so
# readings taken before a trim change are corrected by the change since, rather than thrown away: every step can
# then look at the whole five minutes. Readings from the few seconds just after a change are left out, since the
# devices take a moment to respond.
SAMPLE_EVERY = 5.0  # seconds between readings
AVERAGE_OVER = 300.0  # each device's readings are averaged over this long, in seconds
FULL = AVERAGE_OVER - 2 * SAMPLE_EVERY  # a device needs readings spanning this long to count
AFTER_CHANGE = 10.0  # seconds after a trim change when readings don't count


class ReadingHistory:
    """Each Dante device's recent readings, with the trim at the time, for averaging."""

    def __init__(self) -> None:
        self.readings: dict[str, collections.deque] = {}
        self.changes: collections.deque = collections.deque()  # when the trim changed

    def add(self, now: float, readings: dict[str, int], trim_ppb: int) -> None:
        for device, ppb in readings.items():
            self.readings.setdefault(device, collections.deque()).append((now, ppb, trim_ppb))
        for device in list(self.readings):
            history = self.readings[device]
            while history and history[0][0] < now - AVERAGE_OVER:
                history.popleft()
            if not history:
                del self.readings[device]
        while self.changes and self.changes[0] < now - AVERAGE_OVER:
            self.changes.popleft()

    def trim_changed(self, now: float) -> None:
        self.changes.append(now)

    def averages(self, now: float, devices: Iterable[str], trim_ppb: int, corrected: bool = True) -> dict[str, float]:
        """Each device's average over AVERAGE_OVER, corrected to the current trim, once every one of `devices`
        (those following now) has readings spanning FULL. Until then, nothing: steering by some devices and not
        others would skew the average towards whichever were ready first. Uncorrected, it's what the devices
        actually read, which is what tells whether they're responding to the trim at all."""
        result = {}
        for device in devices:
            history = [(t, ppb, trim) for t, ppb, trim in self.readings.get(device, ())
                       if t >= now - AVERAGE_OVER and not any(0 <= t - c < AFTER_CHANGE for c in self.changes)]
            if not history or now - history[0][0] < FULL:
                return {}
            result[device] = sum(ppb + (trim_ppb - trim if corrected else 0) for _, ppb, trim in history) / len(history)
        return result


# A pause ends by itself after this long, and starts afresh.
RESUME_AFTER = 600.0

# Steps remembered for judging whether the consoles respond.
HISTORY = 16
# If the trim has moved at least this far over recent steps (ppb)...
UNRESPONSIVE_TRIM = 1000
# ...and the consoles have moved less than this fraction of it, they aren't responding.
UNRESPONSIVE_FRACTION = 0.2


def unresponsive(history: list[tuple[int, float]]) -> bool:
    """Whether the consoles have stopped responding to trim changes.

    history holds (trim applied, consoles' average) for recent steps, oldest
    first. A healthy console moves by roughly as much as the trim.
    """
    if len(history) < 2:
        return False
    (first_trim, first_average), (last_trim, last_average) = history[0], history[-1]
    moved = abs(last_trim - first_trim)
    return moved >= UNRESPONSIVE_TRIM and abs(last_average - first_average) < UNRESPONSIVE_FRACTION * moved


# A follower whose chrony jitters more than this (RMS, ns) is still catching
# up, so auto-trim waits for it rather than move the target again.
SETTLED_RMS_NS = 20_000


def followers_settled(clocks: list[dict | None]) -> bool:
    """Whether every follower's chrony is tracking the leader closely."""
    for clock in clocks:
        rms = (clock or {}).get("rms_offset_ns")
        if not isinstance(rms, (int, float)) or abs(rms) > SETTLED_RMS_NS:
            return False
    return True
