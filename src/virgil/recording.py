"""Recordings: everything worth having from a test session, started and stopped from the web UI.

While recording, a Virgil writes into a folder under RECORDINGS:

    status.jsonl       its status every 2 s: clocks, roles, consoles, members, open alerts
    graphs.jsonl       its graphs every 30 s, at full resolution (each covers 40 s, so they overlap)
    diagnostics.jsonl  each console's full netaudio diagnostics every 5 minutes, without histories
    events.jsonl       the event history during the session (alerts, and what the group did)
    journal.log        its timing services' logs
    notes.log          notes added from the web UI, each time-stamped

Status leaves out the event history it normally carries: repeated every 2 s, it was 97% of a night's
log. The events are written once, when the session stops. Diagnostics, at about 9 KB for each of a
console's receive paths, are recorded every 5 minutes for the same reason.

Stopping packs the folder into <name>.tar.gz, in a separate tar process at the lowest CPU and disk
priority: packing a long session takes minutes, and must never crowd out chrony, phc2sys or ptp4l. A
recording stops itself after MAX_SECONDS, or when the disk is down to MIN_FREE_BYTES.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import shutil
import sys
import os
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("virgild.recording")

RECORDINGS = Path("/var/lib/dante-virgil/recordings")
MAX_SECONDS = 24 * 3600
MIN_FREE_BYTES = 500 * 1024 * 1024
STATUS_EVERY = 2.0  # seconds
GRAPHS_EVERY = 15  # status readings (30 s)
DIAGNOSTICS_EVERY = 150  # status readings (5 minutes)
UNITS = ["dante-virgil", "dante-virgil-chrony", "dante-virgil-ptp4l", "dante-virgil-phc2sys",
         "dante-virgil-netaudio", "dante-virgil-trim", "dante-virgil-sync"]
ARCHIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.tar(\.gz)?$")
STAMP = re.compile(r"^[0-9]{8}-[0-9]{6}$")


def stamp_now() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _strip_histories(value: Any) -> Any:
    """Diagnostics without their point histories, which the graphs already have."""
    if isinstance(value, dict):
        return {k: _strip_histories(v) for k, v in value.items() if k not in ("points", "history")}
    if isinstance(value, list):
        return [_strip_histories(v) for v in value]
    return value


def _slim(status: dict) -> dict:
    """Status without the event history it carries: the session's events are written once, when it stops."""
    slim = {key: value for key, value in status.items() if key != "events"}
    if isinstance(slim.get("alerts"), dict):
        slim["alerts"] = {key: value for key, value in slim["alerts"].items() if key != "recent"}
    return slim


PACK = ["nice", "-n", "19", "ionice", "-c", "3", "tar", "-czf"]


SUMMARISE = ["nice", "-n", "19", "ionice", "-c", "3", sys.executable, "-m", "virgil.summary"]


def summary_path(archive: Path) -> Path:
    """Where a saved log's summary is kept, beside it (see virgil.summary)."""
    return archive.with_name(archive.name + ".summary.json")


async def summarise(folder: Path) -> None:
    """Write the session's summary into its folder, in its own process at the lowest CPU and disk priority: a long
    session takes a while to read, and the timing services come first. Done here instead if that can't run."""
    try:
        process = await asyncio.create_subprocess_exec(
            *SUMMARISE, str(folder), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
        _, error = await process.communicate()
        if process.returncode == 0:
            return
        log.warning("Couldn't summarise %s separately (%s); summarising it here", folder.name,
                    error.decode(errors="replace")[-300:])
    except OSError as error:
        log.warning("Couldn't summarise %s separately (%s); summarising it here", folder.name, error)
    from virgil import summary

    try:
        await asyncio.to_thread(summary.write, folder)
    except Exception:
        log.exception("Couldn't summarise %s", folder.name)


async def pack(folder: Path, archive: Path) -> None:
    """Pack a session's folder into a .tar.gz, in its own process at the lowest CPU and disk priority, so the
    timing services always come first. Falls back on Python's tarfile if tar can't run."""
    try:
        process = await asyncio.create_subprocess_exec(
            *PACK, str(archive), "-C", str(folder.parent), folder.name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        if process.returncode == 0:
            return
        log.warning("tar couldn't pack %s (%s); packing it here instead", folder.name, error.decode(errors="replace")[:200])
    except OSError as error:
        log.warning("Couldn't run tar (%s); packing %s here instead", error, folder.name)

    def fallback() -> None:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(folder, arcname=folder.name)

    await asyncio.to_thread(fallback)


def summary_of(archive: Path) -> dict[str, Any] | None:
    """One Virgil's summary of its own log: from the copy beside it, or from inside it (as when a member sent it)."""
    try:
        data = json.loads(summary_path(archive).read_text())
    except (OSError, ValueError):
        data = _summary_inside(archive)
    return data if isinstance(data, dict) and "virgils" not in data else None


def _summary_inside(archive: Path) -> dict[str, Any] | None:
    """The summary from inside a log: a group's at its top, or one Virgil's in its folder."""
    try:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.rsplit("/", 1)[-1] == "summary.json" and member.size < 50_000_000:
                    return json.loads(tar.extractfile(member).read())
    except (OSError, tarfile.TarError, ValueError):
        pass
    return None


class Session:
    def __init__(self, name: str, status: Callable[[], dict], graphs: Callable[[float], list],
                 diagnostics: Callable[[], dict], root: Path = RECORDINGS, units: list[str] | None = UNITS,
                 clock: Callable[[], float] = time.time, events: Callable[[float], list] = lambda since: []):
        self.name = name
        self._status, self._graphs, self._diagnostics = status, graphs, diagnostics
        self._events = events  # the event history since a time, written once when the session stops
        self.root = root
        self.units = units
        self.clock = clock
        self.folder: Path | None = None
        self.since: float | None = None
        self.group = False
        self.notes = 0
        self._task: asyncio.Task | None = None
        self._journal: asyncio.subprocess.Process | None = None

    # State

    @property
    def active(self) -> bool:
        return self.folder is not None

    def size(self) -> int:
        """How much the recording has written so far, before it's compressed."""
        if self.folder is None:
            return 0
        total = 0
        for path in self.folder.iterdir():
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def state(self) -> dict[str, Any]:
        return {"active": self.active, "name": self.folder.name if self.folder else None, "since": self.since,
                "group": self.group, "notes": self.notes, "size": self.size()}

    # Starting and stopping

    async def start(self, stamp: str | None = None, group: bool = False) -> str:
        if self.active:
            raise RuntimeError("already recording")
        stamp = stamp if stamp and STAMP.match(stamp) else stamp_now()
        folder = self.root / f"virgil-{stamp}-{re.sub(r'[^A-Za-z0-9-]+', '-', self.name).strip('-')}"
        folder.mkdir(parents=True, exist_ok=False)
        self.folder, self.since, self.group, self.notes = folder, self.clock(), group, 0
        if self.units:
            try:
                args = ["journalctl", "-f", "-n", "0", "-o", "short-iso-precise"]
                for unit in self.units:
                    args += ["-u", unit]
                self._journal = await asyncio.create_subprocess_exec(
                    *args, stdout=(folder / "journal.log").open("w"), stderr=asyncio.subprocess.DEVNULL)
            except OSError as error:
                log.warning("Recording without the journal: %s", error)
        self._task = asyncio.create_task(self._run())
        log.info("Recording to %s", folder)
        return folder.name

    async def stop(self) -> str | None:
        """Stop, and pack the recording. Returns the archive's file name."""
        if not self.active:
            return None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._journal is not None and self._journal.returncode is None:
            self._journal.terminate()
            try:
                await asyncio.wait_for(self._journal.wait(), 5)
            except TimeoutError:
                self._journal.kill()
        folder, self.folder, self._task, self._journal = self.folder, None, None, None
        try:
            with (folder / "events.jsonl").open("w") as f:
                for event in self._events(self.since or 0.0):
                    f.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as error:
            log.warning("Couldn't write the session's events: %s", error)
        archive = folder.with_name(folder.name + ".tar.gz")
        await summarise(folder)
        await pack(folder, archive)
        try:  # a copy beside the archive, so the web UI can show it without unpacking anything
            shutil.copyfile(folder / "summary.json", summary_path(archive))
        except OSError:
            pass
        shutil.rmtree(folder, ignore_errors=True)
        log.info("Recording saved as %s", archive.name)
        return archive.name

    def note(self, text: str) -> str:
        if not self.active:
            raise RuntimeError("not recording")
        stamp = datetime.datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec="milliseconds")
        with (self.folder / "notes.log").open("a") as f:
            f.write(f"{stamp} {text}\n")
        self.notes += 1
        return stamp

    # The recording itself

    def _append(self, file: str, record: dict) -> None:
        with (self.folder / file).open("a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    async def _run(self) -> None:
        tick = 0
        while True:
            now = self.clock()
            try:
                self._append("status.jsonl", {"t": now, "status": _slim(self._status())})
                if tick % GRAPHS_EVERY == 0:
                    self._append("graphs.jsonl", {"t": now, "series": self._graphs(40)})
                if tick % DIAGNOSTICS_EVERY == 0:
                    self._append("diagnostics.jsonl", {"t": now, "diagnostics": _strip_histories(self._diagnostics())})
            except OSError as error:
                log.warning("Recording stopped: %s", error)
                asyncio.ensure_future(self.stop())
                return
            if now - self.since >= MAX_SECONDS:
                log.info("Recording stopped after %d hours", MAX_SECONDS // 3600)
                asyncio.ensure_future(self.stop())
                return
            if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
                log.warning("Recording stopped: the disk is nearly full")
                asyncio.ensure_future(self.stop())
                return
            tick += 1
            await asyncio.sleep(STATUS_EVERY)

    # Recordings on disk

    def recordings(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        found = []
        for path in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and ARCHIVE.match(path.name):
                stat = path.stat()
                found.append({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
        return found

    def path(self, name: str) -> Path:
        """A recording's path, refusing anything that isn't a plain archive name in the folder."""
        if not ARCHIVE.match(name):
            raise FileNotFoundError(name)
        path = self.root / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def delete(self, name: str) -> None:
        path = self.path(name)
        path.unlink()
        summary_path(path).unlink(missing_ok=True)

    def summary(self, name: str) -> dict[str, Any] | None:
        """A saved log's summary, as {"virgils": [...]}: from the copy beside it, or from inside the log itself."""
        path = self.path(name)
        try:
            data = json.loads(summary_path(path).read_text())
        except (OSError, ValueError):
            data = _summary_inside(path)
        if isinstance(data, dict) and "virgils" not in data:
            data = {"virgils": [data]}  # one Virgil's own
        return data if isinstance(data, dict) else None
