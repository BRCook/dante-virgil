"""Replaying a saved log: what every Virgil in it showed, at any moment of the session.

A day's log is several hundred megabytes unpacked, too much to read through on every
move of the web UI's slider. So a log is first prepared, once, in its own process at
the lowest CPU and disk priority (as packing a log is): unpacked under CACHE, along
with every member's log inside a group's, and indexed: where each status reading and
each logged graph window starts in its file. Preparing also builds an overview of the
whole session (each Dante device's worst-flow latency, and its late packets, in
OVERVIEW_SLICES slices), and gathers the notes and events. index.json, written last,
marks a prepared log as ready.

After that, any moment can be reached directly: each Virgil's status reading nearest
before it, and the graphs for a window ending at it, thinned exactly as the live
page's are (by the same Recorder), so the page draws them with the same code.

Only one log is kept prepared at a time; preparing another, or deleting the log,
removes it. It's never prepared when the disk would be left short.

Run as `python -m virgil.replay prepare <log> <folder>`.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import json
import logging
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from virgil.graphs import Recorder, combine_for

log = logging.getLogger(__name__)

CACHE = Path("/var/lib/dante-virgil/replay")
OVERVIEW_SLICES = 800
MAX_WINDOW = 600.0  # seconds, as the live graphs
GRAPH_SPAN = 40.0  # each logged graph window covers this long (see virgil.recording)
MIN_FREE_BYTES = 500 * 1024 * 1024  # left after unpacking, as when logging
GZIP_RATIO = 12  # a guess at how much a log grows when unpacked, before it's unpacked
PREPARE = ["nice", "-n", "19", "ionice", "-c", "3", sys.executable, "-m", "virgil.replay", "prepare"]


# Preparing a log (in its own process).

def _safe_extract(tar: tarfile.TarFile, into: Path) -> None:
    """Only plain files and folders, and only inside `into`: a log is trusted, but a damaged one shouldn't wander."""
    into = into.resolve()
    for member in tar.getmembers():
        target = (into / member.name).resolve()
        if not (member.isfile() or member.isdir()) or not target.is_relative_to(into):
            continue
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(member) as source, target.open("wb") as out:
            shutil.copyfileobj(source, out, 1024 * 1024)


def _unpack(archive: Path, into: Path) -> list[Path]:
    """Unpack a log, and any member's log inside it. Returns each Virgil's session folder."""
    with tarfile.open(archive) as tar:
        _safe_extract(tar, into)
    for inner in sorted(into.glob("*.tar.gz")):
        with tarfile.open(inner) as tar:
            _safe_extract(tar, into)
        inner.unlink()
    return sorted(p.parent for p in into.glob("*/status.jsonl"))


def _offsets(path: Path) -> list[list[float]]:
    """Each line's time and where it starts, in order of time."""
    found = []
    try:
        with path.open("rb") as f:
            while True:
                at = f.tell()
                line = f.readline()
                if not line:
                    break
                try:
                    t = json.loads(line).get("t")
                except ValueError:
                    continue
                if isinstance(t, (int, float)):
                    found.append([t, at])
    except OSError:
        pass
    found.sort()
    return found


def _overview(folder: Path, start: float, end: float) -> dict[str, dict[str, Any]]:
    """Each Dante device's worst-flow latency (the worst in each slice) and late packets (the count in each)."""
    width = max((end - start) / OVERVIEW_SLICES, 1e-9)
    devices: dict[str, dict[str, Any]] = {}
    previous = -float("inf")
    late_before: dict[str, float] = {}
    with (folder / "graphs.jsonl").open() as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            t0 = record.get("t")
            if not isinstance(t0, (int, float)):
                continue
            since, previous = previous, t0
            for s in record.get("series") or []:
                key = s.get("key") or ""
                if not key.startswith(("latency/", "late/")):
                    continue
                label = (s.get("label") or key).removesuffix(" worst flow")
                d = devices.setdefault(label, {"worst_ns": [None] * OVERVIEW_SLICES, "late": [0.0] * OVERVIEW_SLICES})
                for rel, value in s.get("points") or []:
                    t = t0 + rel
                    if t <= since or not isinstance(value, (int, float)):
                        continue
                    i = min(OVERVIEW_SLICES - 1, max(0, int((t - start) / width)))
                    if key.startswith("latency/"):
                        d["worst_ns"][i] = value if d["worst_ns"][i] is None else max(d["worst_ns"][i], value)
                    else:
                        before = late_before.get(key)
                        if before is not None and 0 < t - before <= 60:
                            d["late"][i] += value * (t - before)  # a rate since the reading before
                        late_before[key] = t
    for d in devices.values():
        d["late"] = [round(n) for n in d["late"]]
    return devices


def prepare(archive: Path, into: Path) -> dict[str, Any]:
    """Unpack and index a log for replay; index.json, written last, says it's ready."""
    progress = into / "progress.json"
    into.mkdir(parents=True, exist_ok=True)
    progress.write_text(json.dumps({"phase": "unpacking"}))
    folders = _unpack(archive, into)
    progress.write_text(json.dumps({"phase": "indexing"}))
    virgils, events, notes = [], [], []
    for folder in folders:
        status = _offsets(folder / "status.jsonl")
        graphs = _offsets(folder / "graphs.jsonl")
        name = folder.name
        if status:
            with (folder / "status.jsonl").open() as f:
                f.seek(int(status[0][1]))
                name = json.loads(f.readline()).get("status", {}).get("name") or name
        virgils.append({"virgil": name, "folder": folder.relative_to(into).as_posix(), "status": status, "graphs": graphs})
        try:
            for line in (folder / "events.jsonl").read_text().splitlines():
                event = json.loads(line)
                if isinstance(event, dict) and isinstance(event.get("t"), (int, float)):
                    events.append({**event, "virgil": name})
        except (OSError, ValueError):
            pass
        try:
            for line in (folder / "notes.log").read_text().splitlines():
                at, _, text = line.partition(" ")
                notes.append({"at": at, "text": text, "virgil": name})
        except OSError:
            pass
    times = [t for v in virgils for t, _ in v["status"]]
    start, end = (min(times), max(times)) if times else (0.0, 0.0)
    overview: dict[str, Any] = {}
    for v in virgils:
        if (into / v["folder"] / "graphs.jsonl").exists():
            overview.update(_overview(into / v["folder"], start, end))
    index = {"name": archive.name, "from": start, "to": end, "virgils": virgils, "slices": OVERVIEW_SLICES,
             "overview": overview, "events": sorted(events, key=lambda e: e["t"]), "notes": notes}
    (into / "index.json.part").write_text(json.dumps(index))
    os.replace(into / "index.json.part", into / "index.json")
    progress.unlink(missing_ok=True)
    return index


# Answering, in virgild.

def _read_line(path: Path, offset: float) -> dict[str, Any] | None:
    try:
        with path.open() as f:
            f.seek(int(offset))
            record = json.loads(f.readline())
            return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


class RecentRecords:
    """Logged graph windows recently read, parsed: consecutive moments (playing, or dragging along the timeline)
    mostly share their windows, so each only needs to read what's new."""

    def __init__(self, size: int = 256):
        self.size, self.records = size, collections.OrderedDict()

    def read(self, path: Path, offset: float) -> dict[str, Any] | None:
        key = (str(path), offset)
        if key in self.records:
            self.records.move_to_end(key)
            return self.records[key]
        record = _read_line(path, offset)
        self.records[key] = record
        if len(self.records) > self.size:
            self.records.popitem(last=False)
        return record


def moment(folder: Path, virgil: dict[str, Any], t: float, window: float,
           read=_read_line) -> tuple[dict | None, float | None, list[dict]]:
    """One Virgil at time t: its status reading nearest before (or at) t and how old it is then, and its graphs for
    `window` up to t (read through `read`, which may remember them)."""
    status, age = None, None
    times = [x[0] for x in virgil["status"]]
    i = bisect.bisect_right(times, t) - 1
    if i >= 0:
        record = _read_line(folder / "status.jsonl", virgil["status"][i][1])
        status = record.get("status") if record else None
        age = t - virgil["status"][i][0]
    recorder = Recorder()
    graph_times = [x[0] for x in virgil["graphs"]]
    first = bisect.bisect_left(graph_times, t - window)
    last = bisect.bisect_right(graph_times, t + GRAPH_SPAN)
    previous = virgil["graphs"][first - 1][0] if first > 0 else -float("inf")
    for record_t, offset in virgil["graphs"][first:last]:
        record = read(folder / "graphs.jsonl", offset)
        since, previous = previous, record_t
        if not record:
            continue
        for s in record.get("series") or []:
            key = s.get("key")
            if not isinstance(key, str):
                continue
            for rel, value in s.get("points") or []:
                at = record_t + rel
                if since < at <= t:  # each reading once (the logged windows overlap), and none after t
                    recorder.add(key, s.get("chart"), s.get("label"), s.get("unit"), at, value, combine_for(key))
    return status, age, recorder.window(window, t)


class Replays:
    """The log prepared for replay (one at a time), and preparing it, for virgild."""

    def __init__(self, root: Path, cache: Path = CACHE):
        self.root, self.cache = root, cache
        self._process: asyncio.subprocess.Process | None = None
        self._preparing: str | None = None
        self._failed: dict[str, str] = {}
        self._index: dict[str, Any] | None = None
        self._recent = RecentRecords()

    def _folder(self, name: str) -> Path:
        return self.cache / name

    def state(self, name: str) -> dict[str, Any]:
        if (self._folder(name) / "index.json").exists():
            return {"state": "ready"}
        if self._preparing == name and self._process is not None and self._process.returncode is None:
            try:
                phase = json.loads((self._folder(name) / "progress.json").read_text()).get("phase")
            except (OSError, ValueError):
                phase = "starting"
            return {"state": "preparing", "phase": phase}
        if name in self._failed:
            return {"state": "failed", "error": self._failed[name]}
        return {"state": "none"}

    def forget(self, name: str | None = None) -> None:
        """Remove a prepared log (or all of them), e.g. when the log is deleted, stopping any preparing it."""
        if (name is None or self._preparing == name) and self._process is not None and self._process.returncode is None:
            self._process.kill()
            self._preparing = None
        if name is None:
            shutil.rmtree(self.cache, ignore_errors=True)
        else:
            shutil.rmtree(self._folder(name), ignore_errors=True)
        if self._index and (name is None or self._index.get("name") == name):
            self._index = None
        self._recent = RecentRecords()

    def _room_for(self, archive: Path) -> bool:
        """Whether unpacking the log would leave MIN_FREE_BYTES: its own size, or a guess for packed members' logs."""
        try:
            with tarfile.open(archive) as tar:
                need = sum(m.size * (GZIP_RATIO if m.name.endswith(".tar.gz") else 1) for m in tar.getmembers())
            if archive.name.endswith(".tar.gz"):
                need *= GZIP_RATIO // 2
            self.cache.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.cache).free - need >= MIN_FREE_BYTES
        except (OSError, tarfile.TarError):
            return False

    async def prepare(self, archive: Path) -> dict[str, Any]:
        """Start preparing a log, if it isn't ready or already being prepared. Only one log is kept prepared."""
        name = archive.name
        state = self.state(name)
        if state["state"] in ("ready", "preparing"):
            return state
        if self._process is not None and self._process.returncode is None:
            self._process.kill()  # a different log: one at a time
            await self._process.wait()
        for other in self.cache.glob("*") if self.cache.exists() else ():
            if other.name != name:
                shutil.rmtree(other, ignore_errors=True)
        self._index = None
        self._failed.pop(name, None)
        if not self._room_for(archive):
            self._failed[name] = "there isn't enough free disk space to unpack this log"
            return self.state(name)
        shutil.rmtree(self._folder(name), ignore_errors=True)
        self._preparing = name
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        self._process = await asyncio.create_subprocess_exec(
            *PREPARE, str(archive), str(self._folder(name)), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, env=env)
        from virgil.tasks import spawn

        spawn(self._finish(name, self._process), f"Preparing {name} for replay")
        return self.state(name)

    async def _finish(self, name: str, process: asyncio.subprocess.Process) -> None:
        _, error = await process.communicate()
        if self._preparing == name:
            self._preparing = None
        if process.returncode != 0 and not (self._folder(name) / "index.json").exists():
            message = error.decode(errors="replace").strip().splitlines()
            self._failed[name] = message[-1] if message else f"preparing it stopped ({process.returncode})"
            log.warning("Couldn't prepare %s for replay: %s", name, self._failed[name])
            shutil.rmtree(self._folder(name), ignore_errors=True)

    def index(self, name: str) -> dict[str, Any] | None:
        if self._index is None or self._index.get("name") != name:
            try:
                self._index = json.loads((self._folder(name) / "index.json").read_text())
            except (OSError, ValueError):
                return None
        return self._index

    def overview(self, name: str) -> dict[str, Any] | None:
        """The whole session, for the timeline: its span, each device's overview, the notes and events."""
        index = self.index(name)
        if index is None:
            return None
        return {key: index[key] for key in ("name", "from", "to", "slices", "overview", "events", "notes")} | {
            "virgils": [v["virgil"] for v in index["virgils"]]}

    def at(self, name: str, t: float, window: float) -> dict[str, Any] | None:
        """Every Virgil in the log at time t: its status, and its graphs for the window ending at t, merged."""
        index = self.index(name)
        if index is None:
            return None
        window = max(1.0, min(MAX_WINDOW, window))
        statuses, series = {}, []
        for virgil in index["virgils"]:
            status, age, graphs = moment(self._folder(name) / virgil["folder"], virgil, t, window, self._recent.read)
            # How old each reading is at t: one well over the two seconds between readings means that Virgil
            # wasn't logging then (before it started, or after it stopped).
            statuses[virgil["virgil"]] = {"status": status, "age": age}
            series += graphs
        return {"t": t, "window": window, "statuses": statuses, "series": series}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "prepare":
        sys.exit("usage: python -m virgil.replay prepare <log> <folder>")
    started = time.monotonic()
    prepare(Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"prepared in {time.monotonic() - started:.1f} s")
