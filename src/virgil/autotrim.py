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

from collections.abc import Iterable

INTERVAL = 30.0
# Correct this fraction of the consoles' average offset each step.
GAIN = 0.3
# Leave the trim alone within this band, in ppb.
DEADBAND = 200
# The largest change in one step, in ppb.
MAX_STEP = 500
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
    step = max(-MAX_STEP, min(MAX_STEP, round(-GAIN * average)))
    new = max(-MAX_TRIM, min(MAX_TRIM, trim_ppb + step))
    return None if new == trim_ppb else new
