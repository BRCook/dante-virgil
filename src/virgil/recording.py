"""Recordings: everything worth having from a test session, started and stopped from the web UI.

While recording, a Virgil writes into a folder under RECORDINGS:

    status.jsonl       its status every 2 s: clocks, roles, consoles, members, alerts
    graphs.jsonl       its graphs every 30 s, at full resolution (each covers 40 s, so they overlap)
    diagnostics.jsonl  each console's current diagnostics every 30 s, without histories
    journal.log        its timing services' logs
    notes.log          notes added from the web UI, each time-stamped

Stopping packs the folder into <name>.tar.gz. A recording stops itself after
MAX_SECONDS, or when the disk is down to MIN_FREE_BYTES.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import shutil
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("virgild.recording")

RECORDINGS = Path("/var/lib/dante-virgil/recordings")
MAX_SECONDS = 24 * 3600
MIN_FREE_BYTES = 500 * 1024 * 1024
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


class Session:
    def __init__(self, name: str, status: Callable[[], dict], graphs: Callable[[float], list],
                 diagnostics: Callable[[], dict], root: Path = RECORDINGS, units: list[str] | None = UNITS,
                 clock: Callable[[], float] = time.time):
        self.name = name
        self._status, self._graphs, self._diagnostics = status, graphs, diagnostics
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
        archive = folder.with_name(folder.name + ".tar.gz")

        def pack() -> None:
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(folder, arcname=folder.name)
            shutil.rmtree(folder, ignore_errors=True)

        await asyncio.to_thread(pack)
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
                self._append("status.jsonl", {"t": now, "status": self._status()})
                if tick % 15 == 0:
                    self._append("graphs.jsonl", {"t": now, "series": self._graphs(40)})
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
            await asyncio.sleep(2.0)

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
        self.path(name).unlink()
