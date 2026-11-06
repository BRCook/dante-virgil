"""virgil-root: the only part of Virgil that runs as root.

It's run by Virgil's systemd units, never by hand:

  prepare  Read the daemon's request, check it, and generate the configuration
           for chrony, ptp4l and phc2sys into a directory only root can write.
  sync     Set the clocks before the timing services start.
  hwstamp  Turn on hardware timestamping, for NICs whose clock only runs once
           it's on.

sync and hwstamp only read what prepare generated, never the request itself.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from virgil.files import UnsafeFileError, atomic_write, read_small_file
from virgil.generate import (
    Command,
    Role,
    TimingSetup,
    chrony_conf,
    hwstamp_ctl_args,
    phc2sys_args,
    ptp4l_conf,
    sync_commands,
)
from virgil.interfaces import Interface, Timestamping, get_interface
from virgil.request import MAX_REQUEST_BYTES, REQUEST_PATH, RequestError, from_json

GENERATED_DIR = Path("/run/dante-virgil-generated")
MAX_PLAN_BYTES = 64 * 1024


class PrepareError(Exception):
    pass


def resolve_setup(
    interface_name: str,
    trim_ppb: int,
    use_nic_profile: bool,
    lookup: Callable[[str], Interface | None] = get_interface,
) -> TimingSetup:
    """Work out the PHC and profile from the hardware, not from the request."""
    interface = lookup(interface_name)
    if interface is None:
        raise PrepareError(f"no interface called {interface_name}")
    if not interface.physical:
        raise PrepareError(f"{interface_name} isn't a physical interface")
    if interface.mode is Timestamping.NONE:
        raise PrepareError(f"{interface_name} can't timestamp PTP packets at all")
    phc = interface.timestamping.phc_device if interface.mode is Timestamping.HARDWARE else None
    return TimingSetup(
        ptp_interface=interface_name,
        phc=phc,
        profile=interface.profile if use_nic_profile else None,
        trim_ppb=trim_ppb,
    )


def prepare(
    request_path: Path = REQUEST_PATH,
    generated: Path = GENERATED_DIR,
    lookup: Callable[[str], Interface | None] = get_interface,
) -> None:
    request = from_json(read_small_file(request_path, MAX_REQUEST_BYTES))
    setup = resolve_setup(request.ptp_interface, request.trim_ppb, request.use_nic_profile, lookup)

    chrony = chrony_conf(request.role, setup, list(request.allow), request.leader, request.tuning)
    phc2sys = phc2sys_args(setup, request.tuning) if request.role is Role.FOLLOWER else None
    hwstamp = hwstamp_ctl_args(setup)
    plan = {
        "role": request.role.value,
        "sync": [{"args": list(c.args), "may_fail": c.may_fail} for c in sync_commands(setup, request.leader)],
        "hwstamp": ["/usr/sbin/hwstamp_ctl", *hwstamp] if hwstamp else None,
    }

    atomic_write(generated / "ptp4l.conf", ptp4l_conf(setup, request.tuning).encode())
    atomic_write(generated / "chrony.conf", chrony.encode())
    env = generated / "phc2sys.env"
    if phc2sys is None:
        # The phc2sys unit won't start without this file.
        env.unlink(missing_ok=True)
    else:
        atomic_write(env, f'PHC2SYS_ARGS="{" ".join(phc2sys)}"\n'.encode())
    # Written last: sync and hwstamp read only this.
    atomic_write(generated / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())


def _plan(generated: Path) -> dict:
    return json.loads(read_small_file(generated / "plan.json", MAX_PLAN_BYTES))


Runner = Callable[[Sequence[str]], int]


def _run(args: Sequence[str]) -> int:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args).returncode


def sync(generated: Path = GENERATED_DIR, run: Runner = _run) -> int:
    for entry in _plan(generated)["sync"]:
        command = Command(tuple(entry["args"]), entry["may_fail"])
        status = run(command.args)
        if status != 0:
            if command.may_fail:
                print(f"{command.args[0]} failed ({status}); carrying on", flush=True)
                continue
            print(f"{command.args[0]} failed ({status})", file=sys.stderr)
            return status
    return 0


def hwstamp(generated: Path = GENERATED_DIR, run: Runner = _run) -> int:
    command = _plan(generated)["hwstamp"]
    if command is None:
        return 0
    return run(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="virgil-root", description="Virgil's root helper. Run by systemd.")
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--generated", type=Path, default=GENERATED_DIR, help=argparse.SUPPRESS)
    parser.add_argument("command", choices=["prepare", "sync", "hwstamp"])
    args = parser.parse_args(argv)

    try:
        if args.command == "prepare":
            prepare(args.request, args.generated)
            return 0
        if args.command == "sync":
            return sync(args.generated)
        return hwstamp(args.generated)
    except (RequestError, PrepareError, UnsafeFileError, OSError) as error:
        print(f"virgil-root: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
