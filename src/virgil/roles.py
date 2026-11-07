"""Moving a Virgil between roles by starting, stopping and restarting its units.

The steps for each change are worked out by transition(), which is a pure
function, and carried out by apply(). The daemon will use these; for now,
virgilctl does.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from virgil.files import UnsafeFileError, atomic_write, read_small_file
from virgil.generate import Role
from virgil.request import MAX_REQUEST_BYTES, REQUEST_PATH, RequestError, TimingRequest, from_json, to_json

PREPARE = "dante-virgil-prepare.service"
SYNC = "dante-virgil-sync.service"
CHRONY = "dante-virgil-chrony.service"
PTP4L = "dante-virgil-ptp4l.service"
PHC2SYS = "dante-virgil-phc2sys.service"
TRIM = "dante-virgil-trim.service"

PTP4L_TUNING = ("log_sync_interval", "log_announce_interval", "log_min_delay_req_interval")
PHC2SYS_TUNING = ("phc2sys_kp", "phc2sys_ki")


class ServiceError(Exception):
    pass


@dataclass(frozen=True)
class Step:
    action: str  # "write", "start", "stop" or "restart"
    unit: str | None = None
    # False for units that may take a while to start, such as phc2sys
    # waiting for chrony to sync.
    block: bool = True


WRITE = Step("write")


def _start(unit: str, block: bool = True) -> Step:
    return Step("start", unit, block)


def _stop(unit: str) -> Step:
    return Step("stop", unit)


def _restart(unit: str, block: bool = True) -> Step:
    return Step("restart", unit, block)


def _changed(old: TimingRequest, new: TimingRequest, names: tuple[str, ...]) -> bool:
    return any(getattr(old.tuning, name) != getattr(new.tuning, name) for name in names)


def transition(old: TimingRequest | None, new: TimingRequest | None) -> list[Step]:
    """The steps that take a Virgil from one request to another. None means stopped."""
    if new is None:
        return [_stop(PHC2SYS), _stop(PTP4L), _stop(CHRONY)]

    steps = [WRITE, _start(PREPARE)]

    # A different PHC, or different NIC settings, need the clocks set up from
    # scratch, which the console will notice.
    if old is None or old.ptp_interface != new.ptp_interface or old.use_nic_profile != new.use_nic_profile:
        steps += [_stop(PHC2SYS), _stop(PTP4L), _stop(CHRONY), _start(SYNC), _start(CHRONY)]
        if new.role is Role.FOLLOWER:
            steps.append(_start(PHC2SYS, block=False))
        steps.append(_start(PTP4L))
        return steps

    # The trim changes a leader's PHC rate in place, with nothing restarted.
    # A follower's rate belongs to phc2sys, so its trim only matters at the next sync.
    retrim = old.trim_ppb != new.trim_ppb and new.role is Role.LEADER
    if replace(old, trim_ppb=new.trim_ppb) == new:
        return steps + ([_start(TRIM)] if retrim else [])

    if new.role is Role.LEADER:
        # The PHC keeps the rate phc2sys last gave it, so stopping phc2sys
        # doesn't move it. A new trim only takes over once phc2sys has stopped.
        steps.append(_stop(PHC2SYS))
        if retrim:
            steps.append(_start(TRIM))
        steps.append(_restart(CHRONY))
    else:
        steps.append(_restart(CHRONY))
        if old.role is Role.LEADER:
            steps.append(_start(PHC2SYS, block=False))
        elif _changed(old, new, PHC2SYS_TUNING):
            steps.append(_restart(PHC2SYS, block=False))

    # ptp4l is never restarted by a role change, only by a change to its own settings.
    steps.append(_restart(PTP4L) if _changed(old, new, PTP4L_TUNING) else _start(PTP4L))
    return steps


class Systemd:
    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._run = run

    def _systemctl(self, *args: str) -> None:
        result = self._run(["systemctl", *args], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            raise ServiceError(f"systemctl {' '.join(args)} failed" + (f": {detail}" if detail else ""))

    def start(self, unit: str, block: bool = True) -> None:
        self._systemctl("start", *(() if block else ("--no-block",)), unit)

    def stop(self, unit: str) -> None:
        self._systemctl("stop", unit)

    def restart(self, unit: str, block: bool = True) -> None:
        self._systemctl("restart", *(() if block else ("--no-block",)), unit)

    def is_active(self, unit: str) -> bool:
        return self._run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def write_request(request: TimingRequest, path: Path = REQUEST_PATH) -> None:
    atomic_write(path, to_json(request).encode(), mode=0o640)


def apply(
    old: TimingRequest | None,
    new: TimingRequest | None,
    systemd: Systemd,
    write: Callable[[TimingRequest], None] = write_request,
    log: Callable[[str], None] = lambda message: None,
) -> None:
    for step in transition(old, new):
        if step.action == "write":
            log("Writing the request")
            write(new)
        elif step.action == "start":
            log(f"Starting {step.unit}")
            systemd.start(step.unit, step.block)
        elif step.action == "stop":
            log(f"Stopping {step.unit}")
            systemd.stop(step.unit)
        else:
            log(f"Restarting {step.unit}")
            systemd.restart(step.unit, step.block)


def current_request(systemd: Systemd, path: Path = REQUEST_PATH) -> TimingRequest | None:
    """The request the running services were set up from, or None if they aren't running."""
    if not (systemd.is_active(CHRONY) or systemd.is_active(PTP4L)):
        return None
    try:
        return from_json(read_small_file(path, MAX_REQUEST_BYTES))
    except (RequestError, UnsafeFileError):
        return None
