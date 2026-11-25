"""The last few minutes of everything worth graphing, kept in memory.

virgild samples once a second: clock offsets (its own and its members', from
heartbeats), the consoles' clocks, receive latency and late packets, the trim,
and the link's round trip and jitter. Nothing is written to disk; restarting
virgild starts the graphs afresh.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any

KEEP_SECONDS = 600
MAX_POINTS = 300

# How a chart's points are combined when there are more than fit: the worst
# value matters for latency and jitter; the average is truer for the rest.
WORST = "worst"
MEAN = "mean"
# The series whose thinned points keep the worst reading rather than the average, by key prefix. The rest average.
WORST_PREFIXES = ("latency/", "rtt/", "jitter/", "late/")


def combine_for(key: str) -> str:
    return WORST if key.startswith(WORST_PREFIXES) else MEAN


@dataclass
class Series:
    chart: str
    label: str
    unit: str  # "ns", "ppm", "per_s" or "percent"
    combine: str = MEAN
    # Kept by time (see Recorder.forget_before), however often a series is measured: the link is pinged four times
    # a second, and a cap of two readings a second once cut its charts to the last five minutes. The cap is only
    # a backstop against something recording far faster than anything does.
    points: collections.deque = field(default_factory=lambda: collections.deque(maxlen=KEEP_SECONDS * 20))


def _percentile(ordered: list[float], fraction: float) -> float:
    """The value a fraction of the way through sorted readings, interpolating between neighbours."""
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summary(chart: str, key: str, points: list[tuple[float, float]]) -> dict[str, Any] | None:
    """One figure that sums a series up over the window, worked out from every reading, before any thinning.

    What suits each chart: the peak swing for offsets (they wander both ways); the highest for worst latency and
    the link's round trip and jitter (spikes are what hurt); the median, and the spread of the middle 80% of
    readings, for average latency (how steady it is, untroubled by the spikes the worst line already shows); the
    total for late packets; the range for clocks and the trim; and the mean for packet loss.
    """
    values = [v for _, v in points]
    if not values:
        return None
    if chart in ("offset", "phc"):
        return {"kind": "peak", "value": max(values, key=abs)}
    if chart == "latency":
        if key.startswith("average/"):
            ordered = sorted(values)
            return {"kind": "median", "value": _percentile(ordered, 0.5),
                    "spread": _percentile(ordered, 0.9) - _percentile(ordered, 0.1)}
        if key.startswith("latency/"):
            return {"kind": "max", "value": max(values)}
        return None  # the setting doesn't change
    if chart in ("rtt", "jitter"):
        return {"kind": "max", "value": max(values)}
    if chart == "late":
        # Each reading is a rate since the reading before, so the count is each rate times its interval.
        total = sum(rate * (t - before) for (before, _), (t, rate) in zip(points, points[1:]) if 0 < t - before <= 60)
        return {"kind": "total", "value": total}
    if chart in ("console", "trim"):
        return {"kind": "range", "low": min(values), "high": max(values)}
    if chart == "loss":
        return {"kind": "mean", "value": sum(values) / len(values)}
    return None


class Recorder:
    def __init__(self) -> None:
        self.series: dict[str, Series] = {}

    def add(self, key: str, chart: str, label: str, unit: str, t: float, value: float | None,
            combine: str = MEAN) -> None:
        if value is None or isinstance(value, bool):
            return
        series = self.series.get(key)
        if series is None:
            series = self.series[key] = Series(chart, label, unit, combine)
        series.points.append((t, float(value)))

    def forget_before(self, t: float) -> None:
        for key in list(self.series):
            points = self.series[key].points
            while points and points[0][0] < t:
                points.popleft()
            if not points:
                del self.series[key]

    def window(self, seconds: float, now: float, max_points: int = MAX_POINTS) -> list[dict[str, Any]]:
        """Each series over the last `seconds`, at most max_points each, with times relative to now."""
        start = now - seconds
        bucket = max(seconds / max_points, 1e-9)
        result = []
        for key, series in sorted(self.series.items()):
            buckets: dict[int, list[float]] = {}
            inside = [(t, value) for t, value in series.points if t >= start]
            for t, value in inside:
                buckets.setdefault(int((t - start) / bucket), []).append(value)
            if not buckets:
                continue
            points = []
            for index in sorted(buckets):
                values = buckets[index]
                value = max(values, key=abs) if series.combine == WORST else sum(values) / len(values)
                points.append([round(start + (index + 0.5) * bucket - now, 2), round(value, 3)])
            result.append({"key": key, "chart": series.chart, "label": series.label, "unit": series.unit,
                           "points": points, "summary": summary(series.chart, key, inside)})
        return result


def jitter(previous: float | None, rtt: float, last_rtt: float | None) -> float:
    """Interarrival jitter, as RTP measures it (RFC 3550): a smoothed average of how much each round trip differs."""
    if last_rtt is None:
        return 0.0 if previous is None else previous
    current = 0.0 if previous is None else previous
    return current + (abs(rtt - last_rtt) - current) / 16
