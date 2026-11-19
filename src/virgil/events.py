"""The history of what's happened: alerts opening and closing, and things the group did.

Everything goes into one file, /var/lib/dante-virgil/events.jsonl, one event to a
line, which outlasts reboots and is rotated at 1 MB. The latest are kept in memory
for the web UI and `virgilctl events`, and read back from the file at startup.
"""

from __future__ import annotations

import collections
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("virgild.events")

EVENTS_PATH = Path("/var/lib/dante-virgil/events.jsonl")
MAX_EVENTS_BYTES = 1024 * 1024
RECENT = 200

# Group events: what happened, as a label for each kind.
GROUP_LABELS = {
    "joined": "JOINED",
    "left": "LEFT",
    "removed": "REMOVED",
    "leader": "NEW LEADER",
    "time": "TIME SET",
    "ptpv1": "PTPv1",
    "timing": "TIMING",
}


class EventLog:
    def __init__(self, path: Path = EVENTS_PATH, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self.recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=RECENT)
        self._load()

    def _load(self) -> None:
        """The latest events from the file, newest first, so the history outlasts a restart."""
        lines: list[str] = []
        for path in (self.path.with_suffix(".jsonl.1"), self.path):
            try:
                lines += path.read_text().splitlines()
            except OSError:
                continue
        for line in lines[-RECENT:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("t"), (int, float)):
                self.recent.appendleft(event)

    def record(self, event: dict[str, Any]) -> None:
        self.recent.appendleft(event)
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_EVENTS_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            with self.path.open("a") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as error:
            log.warning("Couldn't record the event: %s", error)

    def group(self, kind: str, title: str) -> None:
        """Something the group did, such as a Virgil joining, or a new leader."""
        log.info("%s", title)
        self.record({"t": self.clock(), "event": "group", "kind": kind, "label": GROUP_LABELS.get(kind, kind.upper()),
                     "title": title})
