"""Alerts: lost locks, mutes and audio problems on the local Dante devices.

netaudio's daemon already detects these as "issues" (GET /issues). Virgil
watches the open ones for its own local devices, so two Virgils never report
the same console twice. A new one is logged as an error and shown on every
Virgil's page; when it goes away, that's logged too, with how long it lasted.
Every opening and closing is also kept in events.jsonl, which survives
reboots, for reviewing a show afterwards.
"""

from __future__ import annotations

import collections
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger("virgild.alerts")

EVENTS_PATH = Path("/var/lib/dante-virgil/events.jsonl")
MAX_EVENTS_BYTES = 1024 * 1024
RECENT = 50

# netaudio's kinds of issue, and how Virgil labels them.
LABELS = {
    "clock_synchronization": "LOCK LOST",
    "clock_muted": "AUDIO MUTED",
    "receiver_late_packets": "LATE PACKETS",
    "subscription_failure": "SUBSCRIPTION FAILED",
    "receiver_health_degraded": "RECEIVE PROBLEM",
}


@dataclass(frozen=True)
class Alert:
    id: str
    kind: str
    severity: str  # "error", "warning" or "info"
    label: str
    device: str
    server_name: str
    title: str
    summary: str
    since: str  # when netaudio first saw it

    def summary_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "severity": self.severity, "label": self.label,
                "device": self.device, "title": self.title, "summary": self.summary, "since": self.since}

    def line(self) -> str:
        detail = f" ({self.summary})" if self.summary else ""
        return f"{self.label}: {self.device}: {self.title}{detail}"


def parse_issues(data: Any) -> list[Alert]:
    """netaudio's /issues page, as alerts. Only open issues count."""
    issues = data.get("issues") if isinstance(data, dict) else None
    alerts = []
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict) or issue.get("state") != "open":
            continue
        scope = issue.get("scope") if isinstance(issue.get("scope"), dict) else {}
        kind = str(issue.get("kind") or "unknown")
        severity = str(issue.get("severity") or "info")
        title = str(issue.get("title") or kind.replace("_", " "))
        server_name = str(scope.get("server_name") or "")
        alerts.append(Alert(
            id=str(issue.get("issue_id") or f"{kind}/{server_name}"),
            kind=kind,
            severity=severity,
            label=LABELS.get(kind, title.upper() if severity == "error" else title),
            device=str(scope.get("device_name") or server_name or "a device"),
            server_name=server_name,
            title=title,
            summary=str(issue.get("summary") or ""),
            since=str(issue.get("first_seen") or ""),
        ))
    return alerts


class Alerts:
    """Watches netaudio's issues for this Virgil's local devices."""

    def __init__(self, fetch: Callable, local_servers: Callable[[], set[str]],
                 events_path: Path = EVENTS_PATH, clock: Callable[[], float] = time.time):
        self._fetch = fetch
        self._local = local_servers
        self._events_path = events_path
        self._clock = clock
        self.open: dict[str, Alert] = {}
        self._opened_at: dict[str, float] = {}
        self.recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=RECENT)
        self.supported: bool | None = None

    async def poll(self) -> None:
        try:
            data = await self._fetch()
        except aiohttp.ClientResponseError as error:
            if error.status == 404 and self.supported is not False:
                log.info("This netaudio doesn't report issues (it needs 2.0.0 or later), so there are no alerts")
                self.supported = False
            return
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return  # netaudio is away; leave the open alerts as they are until it's back
        self.supported = True
        local = self._local()
        now = self._clock()
        current = {a.id: a for a in parse_issues(data) if a.server_name in local and a.severity != "info"}
        for alert_id, alert in current.items():
            if alert_id not in self.open:
                self._opened_at[alert_id] = now
                level = logging.ERROR if alert.severity == "error" else logging.WARNING
                log.log(level, "%s", alert.line())
                self._record("opened", alert, now)
        for alert_id in list(self.open):
            if alert_id not in current:
                alert = self.open[alert_id]
                lasted = now - self._opened_at.pop(alert_id, now)
                log.info("Resolved after %.0f s: %s", lasted, alert.line())
                self._record("resolved", alert, now, lasted)
        self.open = current

    def _record(self, what: str, alert: Alert, now: float, lasted: float | None = None) -> None:
        event = {"t": now, "event": what, **alert.summary_dict()}
        if lasted is not None:
            event["lasted_s"] = round(lasted, 1)
        self.recent.appendleft(event)
        try:
            if self._events_path.exists() and self._events_path.stat().st_size > MAX_EVENTS_BYTES:
                self._events_path.replace(self._events_path.with_suffix(".jsonl.1"))
            with self._events_path.open("a") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as error:
            log.warning("Couldn't record the event: %s", error)

    def open_summaries(self) -> list[dict[str, Any]]:
        return [a.summary_dict() for a in sorted(self.open.values(), key=lambda a: (a.severity != "error", a.since))]
