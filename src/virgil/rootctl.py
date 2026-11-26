"""virgil-root: the only part of Virgil that runs as root.

It's run by Virgil's systemd units, never by hand:

  prepare  Read the daemon's request, check it, and generate the configuration
           for chrony, ptp4l and phc2sys into a directory only root can write.
  sync     Set the clocks before the timing services start.
  hwstamp  Turn on hardware timestamping, for NICs whose clock only runs once
           it's on.
  trim     Change a leader's PHC rate to its trim, in place.
  dhcpnames  Name each chosen interface's DHCP lease, in /etc/network/interfaces.
  settime  Move a leader's clock by the correction virgild asked for, and its PHC with it.

sync, hwstamp and trim only read what prepare generated, never the request itself.
"""

from __future__ import annotations

import argparse
import json
import time
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
from virgil.request import INTERFACE_NAME, MAX_REQUEST_BYTES, REQUEST_PATH, RequestError, from_json

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
        # Without ptp4l (the "only" PTPv1 mode), something else has to turn hardware timestamping on.
        "hwstamp": ["/usr/sbin/hwstamp_ctl", *hwstamp] if hwstamp
        else ["/usr/sbin/hwstamp_ctl", "-i", request.ptp_interface, "-t", "1", "-r", "1"]
        if not request.serve_ptpv2 and setup.hardware else None,
        "trim": list(trim.args) if (trim := trim_command(setup)) else None,
        "phc": setup.phc if setup.hardware else None,
        # Measuring the card's transmit timestamp slots, once ptp4l is stopped (see virgil.tsprobe).
        # The profile's own hwstamp_ctl settings, without its "-i <interface>", which probe_slots adds.
        "probe": {"interface": request.ptp_interface,
                  "hwstamp": list(setup.profile.hwstamp_ctl_args) if hwstamp else ["-t", "1", "-r", "1"]}
        if request.probe_timestamps and setup.hardware else None,
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
    """Run a command, and return its exit status. A command that isn't installed is status 127, as in a shell,
    rather than an error: a step that may fail then carries on, and any other step fails with a clear message."""
    print("+", " ".join(args), flush=True)
    try:
        return SYSTEM_RUN(args).returncode
    except FileNotFoundError:
        print(f"{args[0]} isn't installed", flush=True)
        return 127


def sync(generated: Path = GENERATED_DIR, run: Runner = _run, probe_card=None, probe_path: Path | None = None) -> int:
    plan = _plan(generated)
    for entry in plan["sync"]:
        command = Command(tuple(entry["args"]), entry["may_fail"])
        status = run(command.args)
        if status != 0:
            if command.may_fail:
                print(f"{command.args[0]} failed ({status}); carrying on", flush=True)
                continue
            print(f"{command.args[0]} failed ({status})", file=sys.stderr)
            return status
    if plan.get("probe"):
        probe_slots(plan["probe"], run, probe_card, probe_path)
    return 0


def probe_slots(step: dict, run: Runner = _run, card=None, path: Path | None = None) -> None:
    """Measure the PTP interface's transmit timestamp slots, unless this card has been already.

    Never fails the sync: a probe that can't run leaves the card unmeasured, and Virgil treats it as one slot.
    """
    from virgil import timestamping, tsprobe
    from virgil.interfaces import get_interface

    path = path or timestamping.PROBE_PATH
    interface = step.get("interface")
    if not isinstance(interface, str) or not INTERFACE_NAME.fullmatch(interface):
        return
    found = get_interface(interface, timestamping=lambda name: None)
    key = timestamping.card_key(interface, found.driver if found else None)
    if key in timestamping.load_probes(path):
        print(f"{interface}'s transmit timestamp slots were probed before; not probing again", flush=True)
        return
    if card is None:
        if found is None or not found.link_up:
            print(f"Not probing {interface}'s transmit timestamp slots: it has no link", flush=True)
            return
        if run(["/usr/sbin/hwstamp_ctl", "-i", interface, *step.get("hwstamp", ["-t", "1", "-r", "1"])]) != 0:
            print(f"Not probing {interface}'s transmit timestamp slots: couldn't turn hardware timestamping on",
                  flush=True)
            return
    try:
        probe_card = card or tsprobe.RawCard(interface)
        try:
            result = tsprobe.probe(probe_card)
        finally:
            probe_card.close()
    except OSError as error:
        print(f"Couldn't probe {interface}'s transmit timestamp slots: {error}", flush=True)
        return
    try:
        timestamping.save_probe(key, result.as_dict(), path)
    except OSError as error:
        # Such as a directory that isn't root's alone (see timestamping.save_probe). The card stays unmeasured.
        print(f"Couldn't save {interface}'s probe: {error}", flush=True)
        return
    if not result.hardware:
        print(f"{interface} gave no hardware transmit timestamps", flush=True)
    else:
        count = f"at least {result.slots}" if result.slots == result.senders else str(result.slots)
        print(f"{interface}: {count} transmit timestamp slots ({result.senders} senders at once); "
              f"{result.pairs_lost} of {2 * tsprobe.PAIRS} timestamps lost from simultaneous pairs"
              f"{'' if result.pairs_dropped is None else f' ({result.pairs_dropped} by the NIC)'}; "
              f"load {result.losses or 'not tested'}; {'shares safely' if result.shareable else 'cannot share'} "
              f"({result.seconds} s)", flush=True)


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
SYSTEM_RUN = subprocess.run


def _backup_once(path: Path) -> None:
    import shutil

    backup = path.with_name(path.name + ".virgil-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)


def drop_ptp_default_route(ptp: str, run=None) -> None:
    """Remove a default route through the PTP interface now, if another one exists for everything else."""
    run = run or SYSTEM_RUN
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


def dhcpnames(request_path: Path | None = None, interfaces_file: Path | None = None,
              hostname: str | None = None, dhcpcd_conf: Path | None = None, run=None) -> int:
    """Name each chosen interface's DHCP lease, and keep the PTP interface from becoming the default route.

    The real paths and command runner are looked up when called, not when
    defined, so tests can redirect them all in one place.
    """
    request_path = request_path or DHCP_NAMES_REQUEST
    interfaces_file = interfaces_file or INTERFACES_FILE
    dhcpcd_conf = dhcpcd_conf or DHCPCD_CONF
    run = run or SYSTEM_RUN
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
    except ValueError:
        print("virgil-root: the request isn't valid JSON", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("virgil-root: the request must be an object", file=sys.stderr)
        return 1
    # Every name below ends up in /etc/network/interfaces, in dhcpcd.conf or on ifup's command line, as root, so
    # each is checked against the same strict pattern as the timing request's (see virgil.request): this used a
    # looser copy of it, which let through a name ending in a newline, and one starting with "-".
    adding = data.get("add") or []
    if not isinstance(adding, list) or not all(isinstance(a, str) and INTERFACE_NAME.fullmatch(a) for a in adding):
        print("virgil-root: add must be a list of interface names", file=sys.stderr)
        return 1
    wanted = {}
    for key in ("ptp_interface", "mgmt_interface"):
        value = data.get(key)
        if value is not None and not (isinstance(value, str) and INTERFACE_NAME.fullmatch(value)):
            print(f"virgil-root: {key} isn't a valid interface name", file=sys.stderr)
            return 1
        wanted[key] = value
    names = names_module.wanted_names(hostname or socket.gethostname(), wanted["ptp_interface"],
                                      wanted["mgmt_interface"])
    old = interfaces_file.read_text()
    new = old
    configured = names_module.configured_interfaces(names_module.interfaces_texts(interfaces_file) or [old])
    added = [a for a in adding if a not in configured and a in (wanted["ptp_interface"], wanted["mgmt_interface"])]
    for interface in added:
        new = names_module.add_stanza(new, interface)
        print(f"Added a DHCP stanza for {interface}, so it comes up")
    new, notes = names_module.apply(new, names)
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
    for interface in added:
        # Bring it up now. A down interface carries nothing yet, so there's nothing to interrupt.
        try:
            run(["ifup", interface], timeout=90)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"Couldn't bring {interface} up: {error}", file=sys.stderr)
    return 0


SETTIME_REQUEST = Path("/run/dante-virgil/settime.json")
# A correction beyond this is surely a mistake.
MAX_CORRECTION = 20 * 365 * 86400


def settime(request_path: Path | None = None, generated: Path = GENERATED_DIR, run=None,
            set_clock=None, now=time.time) -> int:
    """Move the leader's clock by the requested correction: the system clock, the PHC with it, and the RTC.

    The PHC is the PTP time every console follows, so this makes them re-lock. It's only
    ever asked for from the web UI, after a warning that says so.
    """
    import math

    request_path = request_path or SETTIME_REQUEST
    run = run or _run  # exit statuses, not subprocess results
    set_clock = set_clock or (lambda t: time.clock_settime(time.CLOCK_REALTIME, t))
    try:
        data = json.loads(read_small_file(request_path, 1024))
        request_path.unlink()  # one use only
    except (OSError, ValueError, UnsafeFileError) as error:
        print(f"virgil-root: no time correction to apply: {error}", file=sys.stderr)
        return 1
    delta = data.get("delta") if isinstance(data, dict) else None
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta) \
            or abs(delta) > MAX_CORRECTION:
        print("virgil-root: the time correction isn't a sensible number of seconds", file=sys.stderr)
        return 1
    set_clock(now() + delta)
    print(f"Moved the system clock by {delta:+.3f} s")
    try:
        phc = _plan(generated).get("phc")
    except (OSError, ValueError):
        phc = None
    if phc:
        status = run(["/usr/sbin/phc_ctl", phc, "--", "set"])
        if status != 0:
            print(f"virgil-root: couldn't set {phc} to the new time", file=sys.stderr)
            return status
        print(f"Set {phc} to match")
    run(["/usr/sbin/hwclock", "--systohc"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="virgil-root", description="Virgil's root helper. Run by systemd.")
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--generated", type=Path, default=GENERATED_DIR, help=argparse.SUPPRESS)
    parser.add_argument("command", choices=["prepare", "sync", "hwstamp", "trim", "dhcpnames", "settime"])
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
        if args.command == "settime":
            return settime(generated=args.generated)
        return hwstamp(args.generated)
    except (RequestError, PrepareError, UnsafeFileError, OSError) as error:
        print(f"virgil-root: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
