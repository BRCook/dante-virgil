"""A logging session, summed up: statistics for every measurement, and what happened.

Worked out when a session stops, from every reading in the session's folder:

- graphs.jsonl: every measurement Virgil graphs, at full resolution (in overlapping
  40-second windows, de-duplicated here): offsets, the PHC, the trim, each Dante
  device's frequency offset, latency and late packets, and the link;
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
        if s["chart"] == "loss":
            entry["lost"] = round(sum(v for _, v in points) / 100)  # each ping counts as 0% or 100% lost
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
        "clock": {field: statistics_of(values) for field, values in chrony.items()},
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


def _number(value: float, divisor: float, unit: str | None = None) -> str:
    v = value / divisor
    if unit == "percent":
        return f"{v:,.2f}"  # to hundredths of a percent
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.3g}" if abs(v) < 1 else f"{v:,.2f}"


def _when(t: float | None) -> str:
    return "—" if t is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


CHART_NAMES = {"offset": "Clock offset from the leader", "phc": "PHC offset from system clock", "trim": "Trim",
               "latency": "Receive latency", "late": "Late packets", "console": "Frequency offset",
               "rtt": "Ping round trip", "jitter": "Ping jitter", "loss": "Ping packet loss"}
# Status adds what the graphs don't have. (Its offset from the leader and PHC offset are the graphs' too.)
CLOCK_NAMES = {"rms_offset_ns": ("Clock RMS offset", "ns"), "skew_ppm": ("Clock skew", "ppm"),
               "frequency_ppm": ("Clock frequency", "ppm")}
COLUMNS = ("min", "p1", "p5", "median", "mean", "p95", "p99", "max", "stdev")
SECTIONS = (("Dante devices", ("latency", "late", "console")), ("Ping", ("rtt", "jitter", "loss")),
            ("Clock", ("offset", "phc", "trim")))


def _subject(key: str) -> str:
    """What a series is about: the device, pair or Virgil after its chart's prefix, so its rows group together."""
    return key.split("/", 1)[1] if "/" in key else key


ROW_NAMES = {"latency/": "Receive latency: worst flow", "average/": "Receive latency: average",
             "late": "Late packets", "console": "Frequency offset"}


def _group_of(m: dict[str, Any], section: str) -> tuple[str, str]:
    """Which group a measurement belongs in, and its row's name there (the group's name isn't repeated)."""
    label = m["label"] or ""
    if section == "Dante devices":
        device = label.removesuffix(" worst flow").removesuffix(" average")
        name = next(name for prefix, name in ROW_NAMES.items() if m["key"].startswith(prefix) or m["chart"] == prefix)
        return device, name
    if section == "Ping":
        network = "Dante" if label.endswith("(Dante)") else "Mgmt"
        return network, f"{CHART_NAMES[m['chart']]}: {label.removesuffix(' (Dante)').removesuffix(' (Mgmt)')}"
    return label, m.get("title") or CHART_NAMES.get(m["chart"], m["chart"])  # Clock: grouped by Virgil


def layout(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The whole group's table: sections (Dante devices, Ping, Clock), each of groups (a device, a network, a Virgil),
    each of rows (a measurement: name, unit, stats and a note)."""
    measurements = [m for s in summaries for m in s["measurements"]]
    settings = {_subject(m["key"]): m for m in measurements if m["key"].startswith("limit/")}
    sections = []
    for section, charts in SECTIONS:
        found = [m for m in measurements if m["chart"] in charts and not m["key"].startswith("limit/")]
        if section == "Clock":
            found += [{"chart": "clock", "key": f"clock/{s['virgil']}/{field}", "label": s["virgil"], "unit": unit,
                       "title": title, **(s.get("clock") or s.get("chrony") or {})[field]}
                      for s in summaries for field, (title, unit) in CLOCK_NAMES.items()
                      if (s.get("clock") or s.get("chrony") or {}).get(field)]
        if not found:
            continue
        order = list(charts) + ["clock"]
        groups: dict[str, list[dict[str, Any]]] = {}
        for m in found:
            group, name = _group_of(m, section)
            note = ""
            if "total" in m:
                note = f"total {m['total']:,.0f}"
            elif "lost" in m:
                note = f"lost {m['lost']:,} of {m['count']:,}"
            elif m["key"].startswith("latency/") and _subject(m["key"]) in settings:
                note = f"setting {settings[_subject(m['key'])]['median'] / 1e6:g} ms"
            # Within a group: each pair's (or device's) rows together, in the charts' order, worst flow first, and
            # the clock's own figures in CLOCK_NAMES' order.
            subject = _subject(m["key"]).removesuffix("/dante")
            field = list(CLOCK_NAMES).index(subject.rsplit("/", 1)[1]) if m["chart"] == "clock" else 0
            if m["chart"] == "clock":
                subject = subject.rsplit("/", 1)[0]
            groups.setdefault(group, []).append({"key": m["key"], "label": name, "unit": m["unit"], "stats": m,
                                                 "note": note, "_order": (subject, order.index(m["chart"]), field,
                                                                          not m["key"].startswith("latency/"))})
        # Dante first among the networks (it's the audio's path); devices and Virgils by name.
        names = sorted(groups, key=lambda g: (g != "Dante", g) if section == "Ping" else (g,))
        sections.append({"section": section, "groups": [
            {"group": g, "rows": [{k: v for k, v in r.items() if k != "_order"}
                                  for r in sorted(groups[g], key=lambda r: r["_order"])]}
            for g in names]})
    return sections


def _row(label: str, unit: str | None, stats: dict[str, float], extra: str = "", side: str = "", first: bool = False) -> str:
    divisor, unit_name = _scale(unit, stats["median"])
    if unit == "ns":
        # Times can be shown in any unit: the unit is a button that cycles the row through ns, µs and ms.
        cells = "".join(f'<td data-ns="{float(stats[c])!r}">{_number(stats[c], divisor)}</td>' for c in COLUMNS)
        unit_cell = f'<td class=unit><button type=button title="Change the unit">{html.escape(unit_name)}</button></td>'
    else:
        cells = "".join(f"<td>{_number(stats[c], divisor, unit)}</td>" for c in COLUMNS)
        unit_cell = f"<td class=unit>{html.escape(unit_name)}</td>"
    return (f"<tr{' class=first' if first else ''}>{side}<th class=name>{html.escape(label)}</th>{unit_cell}{cells}"
            f"<td>{stats['count']:,}</td><td>{html.escape(extra)}</td></tr>")


def render(summaries: list[dict[str, Any]]) -> str:
    """A self-contained page: the whole group's measurements in one table, in sections, then what happened."""
    head = ("<th></th><th></th><th>unit</th>" + "".join(f"<th>{c}</th>" for c in COLUMNS) + "<th>readings</th><th></th>")
    columns = len(COLUMNS) + 5
    rows = []
    for section in layout(summaries):
        rows.append(f"<tr class=section><th colspan={columns}>{html.escape(section['section'])}</th></tr>")
        for group in section["groups"]:
            for n, r in enumerate(group["rows"]):
                # The group's name once, down the side, spanning its rows.
                side = (f'<th class=group rowspan={len(group["rows"])}><span>{html.escape(group["group"])}</span></th>'
                        if n == 0 else "")
                rows.append(_row(r["label"], r["unit"], r["stats"], r["note"], side, first=n == 0))
    rows = "".join(rows)

    def late(d: dict) -> str:
        return "—" if d["late_packets"] is None else f"{int(d['late_packets']):,}"

    spans = "".join(
        f"<li><b>{html.escape(s['virgil'])}</b> {_when(s['from'])} to {_when(s['to'])} · "
        + ", ".join(f"{html.escape(str(k))} {100 * float(v) / max(1, s['status_readings']):.1f}%"
                    for k, v in sorted(s["states"].items()))
        + "</li>" for s in summaries)
    devices = "".join(
        f"<tr><th>{html.escape(str(d['name']))} ({html.escape(s['virgil'])})</th>"
        f"<td>{float(d['following_percent']):.1f}%</td><td>{float(d['synchronised_percent']):.1f}%</td><td>{late(d)}</td></tr>"
        for s in summaries for d in s["devices"]) or "<tr><td colspan=4>None</td></tr>"
    alerts = "".join(
        f"<tr><th>{html.escape(str(a['device']))} ({html.escape(s['virgil'])})</th><td>{html.escape(str(a['label']))}</td>"
        f"<td>{int(a['times'])}</td><td>{float(a['seconds']):,.0f} s</td></tr>"
        for s in summaries for a in s["alerts"]) or "<tr><td colspan=4>None</td></tr>"
    events = sorted(((e["t"] or 0, s["virgil"], e) for s in summaries for e in s["events"]), key=lambda x: x[0])
    events_html = "".join(f"<li><span>{_when(e['t'])}</span> {html.escape(virgil)}: <b>{html.escape(str(e['label']))}</b> "
                          f"{html.escape(str(e['title']))}</li>" for _, virgil, e in events) or "<li>None</li>"
    notes = "".join(f"<li><span>{html.escape(n['at'])}</span> {html.escape(n['text'])}</li>"
                    for s in summaries for n in s["notes"]) or "<li>None</li>"
    parts = [f"""<ul>{spans}</ul>
<div class=scroll><table><tr>{head}</tr>{rows}</table></div>
<h2>Dante devices</h2><table><tr><th></th><th>following</th><th>synchronised</th><th>new late packets</th></tr>{devices}</table>
<h2>Alerts</h2><table><tr><th>device</th><th>alert</th><th>times</th><th>total time</th></tr>{alerts}</table>
<h2>Group events</h2><ul>{events_html}</ul>
<h2>Notes</h2><ul>{notes}</ul>"""]
    return f"""<!doctype html><html lang=en><meta charset=utf-8><title>Virgil log summary</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
:root {{ color-scheme: light dark; --muted: #888; }}
body {{ font: 14px/1.45 system-ui, sans-serif; margin: 2rem auto; max-width: 1400px; padding: 0 1rem; }}
table {{ border-collapse: collapse; margin: 0.4rem 0 1rem; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 0.25rem 0.6rem; text-align: right; border-bottom: 1px solid rgba(127,127,127,0.25); }}
th:first-child, th.name {{ text-align: left; font-weight: 500; }}
td.unit {{ color: var(--muted); text-align: left; }}
.scroll {{ overflow-x: auto; }}
ul {{ padding-left: 1.2rem; }} li span {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
tr.section th {{ text-align: left; padding-top: 1.1rem; font-size: 0.8rem; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--muted); border-bottom: 1px solid rgba(127,127,127,0.5); }}
h2 {{ font-size: 1.1rem; margin-top: 1.8rem; }}
th.group {{ width: 1.6rem; padding: 0.3rem 0.2rem; text-align: center; vertical-align: middle; font-weight: 500;
  color: var(--muted); border-right: 1px solid rgba(127,127,127,0.25); }}
th.group span {{ writing-mode: vertical-rl; transform: rotate(180deg); white-space: nowrap; }}
tr.first > * {{ border-top: 1px solid rgba(127,127,127,0.5); }}
td.unit button {{ font: inherit; color: inherit; background: none; border: 1px solid rgba(127,127,127,0.4);
  border-radius: 4px; padding: 0 0.35rem; cursor: pointer; }}
</style>
<h1>Virgil log summary</h1>
{''.join(parts)}
<script>
// Each time row's unit cycles through ns, µs and ms when clicked.
const units = [["ns", 1], ["µs", 1e3], ["ms", 1e6]];
const number = (v) => (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("en")
  : Math.abs(v) < 1 ? Number(v.toPrecision(3)).toString()
  : v.toLocaleString("en", {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}));
document.querySelectorAll("td.unit button").forEach((button) => button.addEventListener("click", () => {{
  const next = units[(units.findIndex(([name]) => name === button.textContent) + 1) % units.length];
  button.textContent = next[0];
  button.closest("tr").querySelectorAll("td[data-ns]").forEach((cell) => {{
    cell.textContent = number(Number(cell.dataset.ns) / next[1]);
  }});
}}));
</script>
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
