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


@dataclass
class Series:
    chart: str
    label: str
    unit: str  # "ns", "ppm", "per_s" or "percent"
    combine: str = MEAN
    points: collections.deque = field(default_factory=lambda: collections.deque(maxlen=KEEP_SECONDS * 2))


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
            for t, value in series.points:
                if t >= start:
                    buckets.setdefault(int((t - start) / bucket), []).append(value)
            if not buckets:
                continue
            points = []
            for index in sorted(buckets):
                values = buckets[index]
                value = max(values, key=abs) if series.combine == WORST else sum(values) / len(values)
                points.append([round(start + (index + 0.5) * bucket - now, 2), round(value, 3)])
            result.append({"key": key, "chart": series.chart, "label": series.label, "unit": series.unit,
                           "points": points})
        return result


def jitter(previous: float | None, rtt: float, last_rtt: float | None) -> float:
    """Interarrival jitter, as RTP measures it (RFC 3550): a smoothed average of how much each round trip differs."""
    if last_rtt is None:
        return 0.0 if previous is None else previous
    current = 0.0 if previous is None else previous
    return current + (abs(rtt - last_rtt) - current) / 16
