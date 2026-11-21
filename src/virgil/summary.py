"""A logging session, summed up: statistics for every measurement, and what happened.

Worked out when a session stops, from every reading in the session's folder:

- graphs.jsonl: every measurement Virgil graphs, at full resolution (in overlapping
  40-second windows, de-duplicated here): offsets, the PHC, the trim, each Dante
  device's clock correction, latency and late packets, and the link;
- status.jsonl: chrony's RMS offset, skew and frequency, and each Dante device's
  state (following this Virgil, synchronised);
- events.jsonl: alerts, and the group's events;
- notes.log: the notes added during the session.

Each measurement gets its count, minimum, maximum, mean, median, standard deviation
and 1st, 5th, 95th and 99th percentiles; late packets also get their total. The
result is written as summary.json (for tools) and summary.html (to read, in any
browser), into the session's folder, so they travel inside the log.

Run as `python -m virgil.summary <folder>`, which is how a session runs it: in its
own process, at the lowest CPU and disk priority, since a long session takes a
while to read.
"""

from __future__ import annotations

import html
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

PERCENTILES = (1, 5, 95, 99)
UNIT_SCALES = {"ns": [(1e6, "ms"), (1e3, "µs"), (1, "ns")]}


def _lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open() as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _percentile(ordered: list[float], fraction: float) -> float:
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def statistics_of(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    stats = {
        "count": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
        "median": _percentile(ordered, 0.5),
        "stdev": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
    }
    for p in PERCENTILES:
        stats[f"p{p}"] = _percentile(ordered, p / 100)
    return stats


def _graph_series(folder: Path) -> dict[str, dict[str, Any]]:
    """Every graphed series, with each reading once. The logged windows overlap (40 seconds, every 30), and their
    times are relative to when each was logged, rounded, so the same reading can differ slightly between two windows:
    each window contributes only what's newer than the end of the one before."""
    series: dict[str, dict[str, Any]] = {}
    previous = -math.inf
    for record in _lines(folder / "graphs.jsonl"):
        t0 = record.get("t")
        if not isinstance(t0, (int, float)):
            continue
        since, previous = previous, t0
        for s in record.get("series") or []:
            key = s.get("key")
            if not isinstance(key, str):
                continue
            entry = series.setdefault(key, {"chart": s.get("chart"), "label": s.get("label"), "unit": s.get("unit"),
                                            "points": {}})
            for point in s.get("points") or []:
                if isinstance(point, list) and len(point) == 2 and isinstance(point[1], (int, float)) \
                        and t0 + point[0] > since:
                    entry["points"][round(t0 + point[0], 3)] = point[1]
    return series


def _late_total(points: list[tuple[float, float]]) -> float:
    """Each late-packet reading is a rate since the reading before, so the count is each rate times its interval."""
    return sum(rate * (t - before) for (before, _), (t, rate) in zip(points, points[1:]) if 0 < t - before <= 60)


def summarise(folder: Path) -> dict[str, Any]:
    name, first, last = folder.name, math.inf, -math.inf
    measurements = []
    for key, s in sorted(_graph_series(folder).items()):
        points = sorted(s["points"].items())
        if not points:
            continue
        first, last = min(first, points[0][0]), max(last, points[-1][0])
        stats = statistics_of([v for _, v in points])
        entry = {"key": key, "chart": s["chart"], "label": s["label"], "unit": s["unit"], **stats}
        if s["chart"] == "late":
            entry["total"] = _late_total(points)
        measurements.append(entry)

    # From status: chrony, and each Dante device's state.
    chrony: dict[str, list[float]] = defaultdict(list)
    devices: dict[str, dict[str, Any]] = {}
    states: dict[str, int] = defaultdict(int)
    readings = 0
    for record in _lines(folder / "status.jsonl"):
        status, t = record.get("status") or {}, record.get("t")
        if not isinstance(t, (int, float)):
            continue
        readings += 1
        first, last = min(first, t), max(last, t)
        name = status.get("name") or name
        states[status.get("state") or "unknown"] += 1
        clock = status.get("clock") or {}
        for field in ("rms_offset_ns", "skew_ppm", "frequency_ppm", "system_offset_ns", "phc_offset_ns"):
            if isinstance(clock.get(field), (int, float)):
                chrony[field].append(clock[field])
        for device in status.get("dante_devices") or []:
            d = devices.setdefault(device.get("name") or "?", {"readings": 0, "following": 0, "synchronised": 0,
                                                                 "late_first": None, "late_last": None})
            d["readings"] += 1
            d["following"] += bool(device.get("follows_this_virgil"))
            d["synchronised"] += device.get("servo") in ("synchronized", "synchronised")
            if isinstance(device.get("late_packets"), int):
                d["late_first"] = device["late_packets"] if d["late_first"] is None else d["late_first"]
                d["late_last"] = device["late_packets"]

    alerts: dict[tuple[str, str], dict[str, Any]] = {}
    group_events = []
    for event in _lines(folder / "events.jsonl"):
        if event.get("event") == "group":
            group_events.append({"t": event.get("t"), "label": event.get("label"), "title": event.get("title")})
        elif event.get("event") == "opened":
            a = alerts.setdefault((str(event.get("device")), str(event.get("label"))),
                                  {"device": event.get("device"), "label": event.get("label"), "times": 0, "seconds": 0.0})
            a["times"] += 1
        elif event.get("event") in ("resolved", "went quiet") and isinstance(event.get("lasted_s"), (int, float)):
            a = alerts.setdefault((str(event.get("device")), str(event.get("label"))),
                                  {"device": event.get("device"), "label": event.get("label"), "times": 0, "seconds": 0.0})
            a["seconds"] += event["lasted_s"]

    notes = []
    try:
        for line in (folder / "notes.log").read_text().splitlines():
            when, _, text = line.partition(" ")
            notes.append({"at": when, "text": text})
    except OSError:
        pass

    return {
        "virgil": name,
        "from": first if first != math.inf else None,
        "to": last if last != -math.inf else None,
        "status_readings": readings,
        "states": dict(states),
        "measurements": measurements,
        "chrony": {field: statistics_of(values) for field, values in chrony.items()},
        "devices": [
            {"name": n, "readings": d["readings"],
             "following_percent": 100 * d["following"] / d["readings"],
             "synchronised_percent": 100 * d["synchronised"] / d["readings"],
             "late_packets": None if d["late_first"] is None else max(0, d["late_last"] - d["late_first"])}
            for n, d in sorted(devices.items())
        ],
        "alerts": sorted(alerts.values(), key=lambda a: (-a["times"], str(a["device"]))),
        "events": group_events,
        "notes": notes,
    }


# Reading it: the same statistics, in units a person reads.

def _scale(unit: str | None, typical: float) -> tuple[float, str]:
    """A unit that suits the typical value (the median), so one outlier can't put a row in different units from
    its neighbours: 786 ms of a reboot still reads as 786,000 µs beside rows of a few hundred µs."""
    if unit in UNIT_SCALES:
        for divisor, name in UNIT_SCALES[unit]:
            if abs(typical) >= divisor * 10 or divisor == 1:
                return divisor, name
    return 1, {"per_s": "/s", "percent": "%"}.get(unit or "", unit or "")


def _number(value: float, divisor: float) -> str:
    v = value / divisor
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.3g}" if abs(v) < 1 else f"{v:,.2f}"


def _when(t: float | None) -> str:
    return "—" if t is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


CHART_NAMES = {"offset": "Clock offset from the leader", "phc": "PHC offset from system clock", "trim": "Trim",
               "latency": "Receive latency", "late": "Late packets", "console": "Device clocks",
               "rtt": "Link round trip", "jitter": "Link jitter", "loss": "Link packet loss"}
CHRONY_NAMES = {"rms_offset_ns": ("chrony RMS offset", "ns"), "skew_ppm": ("chrony skew", "ppm"),
                "frequency_ppm": ("chrony frequency", "ppm"), "system_offset_ns": ("chrony offset", "ns"),
                "phc_offset_ns": ("PHC offset (phc2sys)", "ns")}
COLUMNS = ("min", "p1", "p5", "median", "mean", "p95", "p99", "max", "stdev")


def _row(label: str, unit: str | None, stats: dict[str, float], extra: str = "") -> str:
    divisor, unit_name = _scale(unit, stats["median"])
    cells = "".join(f"<td>{_number(stats[c], divisor)}</td>" for c in COLUMNS)
    return (f"<tr><th>{html.escape(label)}</th><td class=unit>{html.escape(unit_name)}</td>{cells}"
            f"<td>{stats['count']:,}</td><td>{html.escape(extra)}</td></tr>")


def render(summaries: list[dict[str, Any]]) -> str:
    """A self-contained page: one section for each Virgil."""
    head = ("<th></th><th>unit</th>" + "".join(f"<th>{c}</th>" for c in COLUMNS) + "<th>readings</th><th></th>")
    parts = []
    for s in summaries:
        rows = []
        for chart in CHART_NAMES:
            for m in (m for m in s["measurements"] if m["chart"] == chart):
                extra = f"total {m['total']:,.0f}" if "total" in m else ""
                rows.append(_row(f"{CHART_NAMES[chart]}: {m['label']}", m["unit"], m, extra))
        for field, (label, unit) in CHRONY_NAMES.items():
            if s["chrony"].get(field):
                rows.append(_row(label, unit, s["chrony"][field]))
        def late(d: dict) -> str:
            return "—" if d["late_packets"] is None else f"{d['late_packets']:,}"

        devices = "".join(
            f"<tr><th>{html.escape(d['name'])}</th><td>{d['following_percent']:.1f}%</td>"
            f"<td>{d['synchronised_percent']:.1f}%</td><td>{late(d)}</td></tr>"
            for d in s["devices"])
        alerts = "".join(
            f"<tr><th>{html.escape(str(a['device']))}</th><td>{html.escape(str(a['label']))}</td><td>{a['times']}</td>"
            f"<td>{a['seconds']:,.0f} s</td></tr>" for a in s["alerts"]) or "<tr><td colspan=4>None</td></tr>"
        events = "".join(f"<li><span>{_when(e['t'])}</span> <b>{html.escape(str(e['label']))}</b> "
                         f"{html.escape(str(e['title']))}</li>" for e in s["events"]) or "<li>None</li>"
        notes = "".join(f"<li><span>{html.escape(n['at'])}</span> {html.escape(n['text'])}</li>"
                        for n in s["notes"]) or "<li>None</li>"
        states = ", ".join(f"{k} {100 * v / max(1, s['status_readings']):.1f}%" for k, v in sorted(s["states"].items()))
        parts.append(f"""<section><h2>{html.escape(s['virgil'])}</h2>
<p>{_when(s['from'])} to {_when(s['to'])} · {states}</p>
<h3>Measurements</h3><div class=scroll><table><tr>{head}</tr>{''.join(rows)}</table></div>
<h3>Dante devices</h3><table><tr><th></th><th>following</th><th>synchronised</th><th>new late packets</th></tr>{devices}</table>
<h3>Alerts</h3><table><tr><th>device</th><th>alert</th><th>times</th><th>total time</th></tr>{alerts}</table>
<h3>Group events</h3><ul>{events}</ul>
<h3>Notes</h3><ul>{notes}</ul></section>""")
    return f"""<!doctype html><html lang=en><meta charset=utf-8><title>Virgil log summary</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
:root {{ color-scheme: light dark; --muted: #888; }}
body {{ font: 14px/1.45 system-ui, sans-serif; margin: 2rem auto; max-width: 1400px; padding: 0 1rem; }}
table {{ border-collapse: collapse; margin: 0.4rem 0 1rem; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 0.25rem 0.6rem; text-align: right; border-bottom: 1px solid rgba(127,127,127,0.25); }}
th:first-child {{ text-align: left; font-weight: 500; }}
td.unit {{ color: var(--muted); text-align: left; }}
.scroll {{ overflow-x: auto; }}
ul {{ padding-left: 1.2rem; }} li span {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
section {{ margin-bottom: 3rem; }}
</style>
<h1>Virgil log summary</h1>
{''.join(parts)}
</html>
"""


def write(folder: Path) -> dict[str, Any]:
    summary = summarise(folder)
    (folder / "summary.json").write_text(json.dumps(summary, indent=1))
    (folder / "summary.html").write_text(render([summary]))
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m virgil.summary <session folder>")
    write(Path(sys.argv[1]))
