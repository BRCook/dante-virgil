"""virgil-root: the only part of Virgil that runs as root.

It's run by Virgil's systemd units, never by hand:

  prepare  Read the daemon's request, check it, and generate the configuration
           for chrony, ptp4l and phc2sys into a directory only root can write.
  sync     Set the clocks before the timing services start.
  hwstamp  Turn on hardware timestamping, for NICs whose clock only runs once
           it's on.
  trim     Change a leader's PHC rate to its trim, in place.
  dhcpnames  Name each chosen interface's DHCP lease, in /etc/network/interfaces.

sync, hwstamp and trim only read what prepare generated, never the request itself.
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
    trim_command,
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

    hwtimestamp = None
    if request.ntp_hwtimestamp is not None:
        # Only if the interface really does timestamp in hardware.
        interface = lookup(request.ntp_hwtimestamp)
        if interface is not None and interface.physical and interface.mode is Timestamping.HARDWARE:
            hwtimestamp = interface.name
    chrony = chrony_conf(request.role, setup, list(request.allow), request.leader, request.tuning, hwtimestamp)
    phc2sys = phc2sys_args(setup, request.tuning) if request.role is Role.FOLLOWER else None
    hwstamp = hwstamp_ctl_args(setup)
    plan = {
        "role": request.role.value,
        "sync": [{"args": list(c.args), "may_fail": c.may_fail} for c in sync_commands(setup, request.leader)],
        "hwstamp": ["/usr/sbin/hwstamp_ctl", *hwstamp] if hwstamp else None,
        "trim": list(trim.args) if (trim := trim_command(setup)) else None,
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


def trim(generated: Path = GENERATED_DIR, run: Runner = _run) -> int:
    """Change the PHC's rate to the trim in place. Only a leader runs this."""
    command = _plan(generated).get("trim")
    if command is None:
        return 0
    return run(command)


DHCP_NAMES_REQUEST = Path("/run/dante-virgil/dhcp-names.json")
INTERFACES_FILE = Path("/etc/network/interfaces")
DHCPCD_CONF = Path("/etc/dhcpcd.conf")


def _backup_once(path: Path) -> None:
    import shutil

    backup = path.with_name(path.name + ".virgil-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)


def drop_ptp_default_route(ptp: str, run=subprocess.run) -> None:
    """Remove a default route through the PTP interface now, if another one exists for everything else."""
    try:
        result = run(["ip", "-j", "-4", "route", "show", "default"], capture_output=True, text=True)
        routes = json.loads(result.stdout or "[]")
    except (OSError, ValueError):
        return
    through_ptp = [r for r in routes if r.get("dev") == ptp]
    elsewhere = [r for r in routes if r.get("dev") != ptp]
    if through_ptp and elsewhere:
        run(["ip", "-4", "route", "del", "default", "dev", ptp])
        print(f"Removed the default route through {ptp}; {elsewhere[0].get('dev')} still has one")


def dhcpnames(request_path: Path = DHCP_NAMES_REQUEST, interfaces_file: Path = INTERFACES_FILE,
              hostname: str | None = None, dhcpcd_conf: Path = DHCPCD_CONF, run=subprocess.run) -> int:
    """Name each chosen interface's DHCP lease, and keep the PTP interface from becoming the default route."""
    import re
    import shutil
    import socket

    from virgil import dhcpnames as names_module

    if not interfaces_file.exists():
        print("No /etc/network/interfaces (ifupdown isn't in use), so the leases aren't named")
        return 0
    try:
        data = json.loads(read_small_file(request_path, 4096))
    except FileNotFoundError:
        data = {}
    wanted = {}
    for key in ("ptp_interface", "mgmt_interface"):
        value = data.get(key)
        if value is not None and not (isinstance(value, str) and re.match(r"^[A-Za-z0-9_.:-]{1,15}$", value)):
            print(f"virgil-root: {key} isn't a valid interface name", file=sys.stderr)
            return 1
        wanted[key] = value
    names = names_module.wanted_names(hostname or socket.gethostname(), wanted["ptp_interface"],
                                      wanted["mgmt_interface"])
    old = interfaces_file.read_text()
    new, notes = names_module.apply(old, names)
    for note in notes:
        print(note)
    if new != old:
        backup = interfaces_file.with_name(interfaces_file.name + ".virgil-backup")
        if not backup.exists():
            shutil.copy2(interfaces_file, backup)
        atomic_write(interfaces_file, new.encode())
        print("Updated the interfaces file; the names apply the next time each interface comes up")

    old_conf = dhcpcd_conf.read_text() if dhcpcd_conf.exists() else ""
    new_conf = names_module.apply_dhcpcd(old_conf, wanted["ptp_interface"], wanted["mgmt_interface"])
    if new_conf != old_conf:
        _backup_once(dhcpcd_conf)
        atomic_write(dhcpcd_conf, new_conf.encode())
        print("Updated dhcpcd.conf: the PTP interface won't be given a default route from now on")
    if wanted["ptp_interface"] and wanted["ptp_interface"] != wanted["mgmt_interface"]:
        drop_ptp_default_route(wanted["ptp_interface"], run)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="virgil-root", description="Virgil's root helper. Run by systemd.")
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--generated", type=Path, default=GENERATED_DIR, help=argparse.SUPPRESS)
    parser.add_argument("command", choices=["prepare", "sync", "hwstamp", "trim", "dhcpnames"])
    args = parser.parse_args(argv)

    try:
        if args.command == "prepare":
            prepare(args.request, args.generated)
            return 0
        if args.command == "sync":
            return sync(args.generated)
        if args.command == "trim":
            return trim(args.generated)
        if args.command == "dhcpnames":
            return dhcpnames()
        return hwstamp(args.generated)
    except (RequestError, PrepareError, UnsafeFileError, OSError) as error:
        print(f"virgil-root: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
