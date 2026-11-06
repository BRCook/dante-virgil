"""Carrying out the election's decisions on the timing services.

The election decides what this Virgil should be (a leader, a follower of a
particular member, or stopped). TimingManager makes it so, one change at a
time, in the background: it builds the request for virgil-root, and moves the
units from the running request to the new one with roles.apply. If the
election changes its mind while a change is under way, the manager goes
straight on to the latest decision when it finishes.

Listening never stops anything. When virgild starts, the manager adopts
whatever the units are already running, so restarting virgild or updating
Virgil never disturbs a console.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from virgil import roles
from virgil.files import UnsafeFileError, atomic_write, read_small_file
from virgil.generate import Role
from virgil.request import RequestError, TimingRequest, from_json, to_json

STATE_PATH = Path("/run/dante-virgil/state.json")
RETRY_DELAY = 5.0

log = logging.getLogger("virgild.timing")


@dataclass(frozen=True)
class Target:
    role: Role
    # The Virgil ID of the leader, for followers.
    leader_id: str | None = None


Apply = Callable[..., None]


class TimingManager:
    def __init__(
        self,
        build_request: Callable[[Target], TimingRequest | None],
        on_applied: Callable[[Target | None], None],
        systemd: roles.Systemd | None = None,
        request_path: Path = roles.REQUEST_PATH,
        state_path: Path = STATE_PATH,
        apply: Apply = roles.apply,
        retry_delay: float = RETRY_DELAY,
    ):
        self._build = build_request
        self._on_applied = on_applied
        self._systemd = systemd or roles.Systemd()
        self._request_path = request_path
        self._state_path = state_path
        self._apply = apply
        self._retry_delay = retry_delay
        self._wake = asyncio.Event()

        self.request: TimingRequest | None = roles.current_request(self._systemd, request_path)
        self.applied: Target | None = self._load_state() if self.request is not None else None
        self.desired: Target | None = self.applied
        self._acknowledge = False

    def _load_state(self) -> Target | None:
        try:
            data = json.loads(read_small_file(self._state_path, 4096))
            role = Role(data["role"])
            leader = data.get("leader_id")
            return Target(role, leader if isinstance(leader, str) else None)
        except (OSError, UnsafeFileError, ValueError, KeyError, TypeError):
            return None

    def _save_state(self, target: Target | None) -> None:
        if target is None:
            self._state_path.unlink(missing_ok=True)
            return
        data = {"role": target.role.value, "leader_id": target.leader_id}
        atomic_write(self._state_path, (json.dumps(data) + "\n").encode(), mode=0o640)

    def want(self, target: Target | None) -> None:
        """Ask for a role. Takes effect in the background."""
        self.desired = target
        self._acknowledge = True
        self._wake.set()

    def poke(self) -> None:
        """Rebuild the request for the current role, such as after a config change."""
        self._wake.set()

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._settle()

    async def _settle(self) -> None:
        while True:
            target = self.desired
            if target is None:
                new = None
            else:
                try:
                    new = self._build(target)
                except RequestError as error:
                    log.error("Can't set up %s: %s", _describe(target), error)
                    return
                if new is None:
                    # Not possible yet, such as when the leader's address isn't known. Try again soon.
                    await asyncio.sleep(1.0)
                    if self.desired == target:
                        self._wake.set()
                    return
                new = from_json(to_json(new))

            if new != self.request or target != self.applied:
                if new != self.request:
                    log.info("Setting up %s", _describe(target))
                    try:
                        await asyncio.to_thread(self._apply, self.request, new, self._systemd,
                                                write=lambda r: roles.write_request(r, self._request_path),
                                                log=log.info)
                    except roles.ServiceError as error:
                        log.error("Couldn't set up %s: %s. Trying again in %.0f s", _describe(target), error,
                                  self._retry_delay)
                        await asyncio.sleep(self._retry_delay)
                        continue
                    self.request = new
                self.applied = target
                self._save_state(target)
                self._acknowledge = True

            if self.desired != target:
                continue  # The election changed its mind while this was under way.
            if self._acknowledge:
                self._acknowledge = False
                self._on_applied(target)
            return


def _describe(target: Target | None) -> str:
    if target is None:
        return "stopped"
    return target.role.value
