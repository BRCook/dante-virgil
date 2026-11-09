"""virgilctl: Virgil's command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from virgil import __version__
from virgil import config as config_file
from virgil.config import ConfigError
from virgil.files import UnsafeFileError
from virgil import group as groups
from virgil import joining, tls
from virgil.generate import Role
from virgil.group import GroupError, InviteToken
from virgil.control import CONTROL_SOCKET, LEAD_PATH
from virgil.interfaces import Interface, Timestamping, get_interface, ipv4_addresses, list_interfaces
from virgil.request import REQUEST_PATH, RequestError, TimingRequest, from_json, to_json
from virgil.roles import CHRONY, PHC2SYS, PTP4L, ServiceError, Systemd, apply, current_request, write_request
from virgil.setup import SOFTWARE_WARNING, SetupError, check_ptp_interface



class CliError(Exception):
    pass


def confirm(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


# interfaces


def _interface_json(interface: Interface) -> dict:
    ts = interface.timestamping
    profile = interface.profile
    return {
        "name": interface.name,
        "mac": interface.mac,
        "driver": interface.driver,
        "physical": interface.physical,
        "link_up": interface.link_up,
        "timestamping": interface.mode.value,
        "phc": ts.phc_device if ts else None,
        "rx_filter_all_only": ts.rx_filter_all_only if ts else None,
        "profile": None
        if profile is None
        else {
            "description": profile.description,
            "status": profile.status.value,
            "notes": profile.notes,
            "needs_hwstamp_ctl": profile.needs_hwstamp_ctl,
        },
    }


def cmd_interfaces(args: argparse.Namespace) -> int:
    interfaces = list_interfaces()
    if args.json:
        print(json.dumps([_interface_json(i) for i in interfaces], indent=2))
        return 0

    rows = [("NAME", "DRIVER", "LINK", "TIMESTAMPING", "PHC", "PROFILE")]
    for i in interfaces:
        ts = i.timestamping
        profile = i.profile
        rows.append(
            (
                i.name,
                i.driver or "-",
                "up" if i.link_up else "down",
                i.mode.value if i.physical else "virtual",
                (ts.phc_device if ts else None) or "-",
                profile.status.value if profile else ("untested" if i.ptp_capable else "-"),
            )
        )
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())

    notes = [(i, i.profile) for i in interfaces if i.profile is not None]
    if notes:
        print()
        for i, profile in notes:
            print(f"{i.name} ({profile.description}): {profile.notes}")

    if any(i.physical and i.mode is Timestamping.SOFTWARE for i in interfaces):
        print()
        print(f"Interfaces with software timestamping only: {SOFTWARE_WARNING}")

    unknown = [i for i in interfaces if i.ptp_capable and i.profile is None]
    if unknown:
        print()
        print(
            "Untested drivers: "
            + ", ".join(f"{i.name} ({i.driver})" for i in unknown)
            + ". These will use Virgil's defaults."
        )
    return 0


# config


def cmd_config_init(args: argparse.Namespace) -> int:
    if args.config.exists():
        raise CliError(f"{args.config} already exists")
    config = config_file.new_config()
    config_file.save(config, args.config)
    print(f"Created {args.config} for {config.name} ({config.id})")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    print(args.config.read_text(), end="")
    return 0


def _choose_ptp_interface(name: str, assume_yes: bool) -> tuple[str, str | None]:
    """Check a PTP interface with the person, and return it with the NIC profile to use."""
    try:
        choice = check_ptp_interface(name, lookup=get_interface)
        if choice.needs == "software":
            print(choice.message)
            if not confirm("Use it anyway?", default=False, assume_yes=assume_yes):
                raise CliError("not changed")
            choice = check_ptp_interface(name, accept_software=True, lookup=get_interface)
        elif choice.needs == "profile":
            print(choice.message)
            accepted = confirm("Apply them?", default=True, assume_yes=assume_yes)
            choice = check_ptp_interface(name, accept_profile=accepted, lookup=get_interface)
    except SetupError as error:
        raise CliError(str(error)) from error
    if choice.note:
        print(choice.note)
    return choice.name, choice.nic_profile


def cmd_config_set(args: argparse.Namespace) -> int:
    config = config_file.load(args.config)
    key, value = args.key, args.value
    if key == "name":
        config.name = value
    elif key == "ptp_interface":
        config.ptp_interface, config.nic_profile = _choose_ptp_interface(value, args.yes)
    elif key == "mgmt_interface":
        if get_interface(value) is None:
            raise CliError(f"there's no interface called {value}")
        config.mgmt_interface = value
    elif key == "trim_ppb":
        try:
            config.trim_ppb = int(value)
        except ValueError as error:
            raise CliError("trim_ppb must be a whole number") from error
    elif key in ("observe_only", "auto_trim", "ntp_hwtimestamp"):
        if value.lower() not in ("true", "false"):
            raise CliError(f"{key} must be true or false")
        setattr(config, key, value.lower() == "true")
    config_file.save(config, args.config)
    print(f"Set {key}")
    if key in RESTART_KEYS:
        print("Restart virgild for this to take effect: sudo systemctl restart dante-virgil")
    return 0


# Settings virgild only reads when it starts.
RESTART_KEYS = ("observe_only", "ptp_interface", "mgmt_interface", "name")


# role


def _request_for(role: Role, args: argparse.Namespace) -> TimingRequest:
    config = config_file.load(args.config)
    if config.ptp_interface is None:
        raise CliError("choose a PTP interface first: virgilctl config set ptp_interface <name>")
    leader = args.leader if role is Role.FOLLOWER else None
    allow = args.allow or ([leader] if leader else [])
    request = TimingRequest(
        role=role,
        ptp_interface=config.ptp_interface,
        leader=leader,
        allow=tuple(allow),
        trim_ppb=config.trim_ppb,
        use_nic_profile=config.nic_profile is not None,
    )
    # Check it exactly as virgil-root will.
    return from_json(to_json(request))


async def _control(method: str, path: str, socket_path: Path) -> tuple[int, dict]:
    """Talk to the local virgild over its control socket."""
    import aiohttp

    try:
        connector = aiohttp.UnixConnector(path=str(socket_path))
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.request(method, f"http://virgil{path}") as reply:
                return reply.status, await reply.json(content_type=None) or {}
    except PermissionError as error:
        raise PermissionError(error.errno, error.strerror, str(socket_path)) from error
    except (aiohttp.ClientError, OSError) as error:
        raise CliError("couldn't reach virgild. Is it running? (systemctl status dante-virgil)") from error


def cmd_role(args: argparse.Namespace) -> int:
    systemd = Systemd()
    old = current_request(systemd, args.request)
    if args.role == "show":
        if old is None:
            print("Stopped")
        else:
            print(f"Role: {old.role.value}")
            if old.leader:
                print(f"Leader: {old.leader}")
        for unit in (CHRONY, PTP4L, PHC2SYS):
            print(f"  {unit}: {'active' if systemd.is_active(unit) else 'inactive'}")
        return 0

    managed = not config_file.load(args.config).observe_only
    if args.role == "lead":
        if not managed:
            raise CliError("observe_only is set, so virgild doesn't manage roles. Use 'virgilctl role leader'.")
        status, body = asyncio.run(_control("POST", LEAD_PATH, args.control))
        if status >= 300:
            raise CliError(body.get("error", f"virgild refused ({status})"))
        print(body.get("message", "Done"))
        return 0
    if args.role == "stop":
        if managed:
            # Otherwise virgild would start them again.
            print("Stopping virgild, which manages this Virgil's role")
            subprocess.run(["systemctl", "stop", "dante-virgil.service"], check=False)
        apply(old, None, systemd, write=lambda r: write_request(r, args.request), log=print)
        print("Done." + (" Start Virgil again with: sudo systemctl start dante-virgil" if managed else ""))
        return 0
    if managed:
        raise CliError(
            "virgild manages this Virgil's role. To set roles by hand, first run: "
            "sudo virgilctl config set observe_only true"
        )
    new = _request_for(Role(args.role), args)
    apply(old, new, systemd, write=lambda r: write_request(r, args.request), log=print)
    print("Done")
    return 0


# tls


def cmd_tls_init(args: argparse.Namespace) -> int:
    identity = tls.default_identity(args.tls_dir)
    if identity.cert_path.exists():
        print(f"Keeping the existing certificate in {args.tls_dir}")
        return 0
    tls.generate(identity)
    code = tls.short_code(tls.fingerprint(identity.cert_pem))
    print(f"Created a certificate in {args.tls_dir} ({code})")
    return 0


# group


def cmd_group_create(args: argparse.Namespace) -> int:
    config = config_file.load(args.config)
    if config.group is not None:
        raise CliError("this Virgil is already in a group")
    cert = tls.default_identity(args.tls_dir).cert_pem
    config.group = groups.create(config.id, config.name, cert)
    config_file.save(config, args.config)
    print(f"Created group {config.group.id}, with {config.name} as its first member")
    return 0


def cmd_group_invite(args: argparse.Namespace) -> int:
    config = config_file.load(args.config)
    if config.group is None:
        raise CliError("this Virgil isn't in a group. Create one with 'virgilctl group create'")
    if config.mgmt_interface is None:
        raise CliError("choose a management interface first: virgilctl config set mgmt_interface <name>")
    addresses = ipv4_addresses(config.mgmt_interface)
    if not addresses:
        raise CliError(f"{config.mgmt_interface} has no IPv4 address")
    cert = tls.default_identity(args.tls_dir).cert_pem
    secret, expires = groups.create_invite(config.group, time.time(), lifetime=args.minutes * 60)
    config_file.save(config, args.config)
    token = InviteToken(config.group.id, tuple(addresses), groups.PUBLIC_PORT, tls.fingerprint(cert), secret, expires)
    print(token.encode())
    until = datetime.fromtimestamp(expires).strftime("%H:%M")
    print(f"\nThis token can be used once, until {until}. On the new Virgil, run:\n", file=sys.stderr)
    print("  sudo virgilctl group join <token>", file=sys.stderr)
    return 0


def cmd_group_join(args: argparse.Namespace) -> int:
    token = InviteToken.decode(args.token)
    config = config_file.load(args.config)
    cert = tls.default_identity(args.tls_dir).cert_pem
    asyncio.run(joining.join(token, config, cert))
    config_file.save(config, args.config)
    names = sorted(m.name for virgil_id, m in config.group.members.items() if virgil_id != config.id)
    print(f"Joined group {config.group.id}, with {', '.join(names)}")
    return 0


def cmd_group_show(args: argparse.Namespace) -> int:
    config = config_file.load(args.config)
    if config.group is None:
        print("Not in a group")
        return 0
    print(f"Group {config.group.id}")
    for virgil_id, member in sorted(config.group.members.items(), key=lambda item: item[1].name):
        marker = " (this Virgil)" if virgil_id == config.id else ""
        try:
            code = tls.short_code(tls.fingerprint(member.cert))
        except tls.TlsError:
            code = "bad certificate"
        print(f"  {member.name}{marker}  {virgil_id}  {code}")
    pending = [e for e in config.group.invites.values() if e > time.time()]
    if pending:
        print(f"{len(pending)} unused invite(s) outstanding")
    return 0


# status


def fetch_status(tls_dir: Path, port: int = groups.PUBLIC_PORT) -> dict:
    """Ask the local virgild for its status, trusting only this Virgil's own certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    cert_path = tls.default_identity(tls_dir).cert_path
    try:
        context.load_verify_locations(cafile=cert_path)
    except PermissionError as error:
        raise PermissionError(error.errno, error.strerror, str(cert_path)) from error
    try:
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/api/v1/status", context=context, timeout=5) as reply:
            return json.load(reply)
    except (urllib.error.URLError, OSError) as error:
        raise CliError("couldn't reach virgild. Is it running? (systemctl status dante-virgil)") from error


def _describe_device(dante: dict | None, heard: dict | None, virgil: str = "this Virgil") -> str:
    """One line about a Dante device, from netaudio's view if there is one.

    virgil is the Virgil the device belongs to, so a member's console reads
    "following Box B" rather than "following this Virgil".
    """
    following = (dante or heard or {}).get("follows_this_virgil")
    where = (dante or heard or {}).get("address", "?")
    if dante is None:
        return f"Dante device at {where}: {'following' if following else 'NOT following'} {virgil}"
    parts = [f"{dante['name']}" + (f" ({dante['model']})" if dante.get("model") else "") + f" at {where}"]
    parts.append(f"following {virgil}" if following else f"NOT following {virgil}")
    if dante.get("ptpv2") is not None:
        parts.append("PTPv2 " + ("on" if dante["ptpv2"] else "off"))
    if dante.get("aes67") is not None:
        parts.append("AES67 " + ("on" if dante["aes67"] else "off"))
    if dante.get("clock_offset_ppb") is not None:
        parts.append(f"clock {dante['clock_offset_ppb'] / 1000:+.3f} ppm")
    return ", ".join(parts)


def _duration(ns: int | float) -> str:
    """A signed time offset. Always in nanoseconds, so figures can be compared at a glance."""
    return f"{ns:+,.0f} ns"


def _describe_clock(clock: dict | None, names: dict[str, str], leader: str | None = None) -> list[str]:
    """Lines describing what chrony and phc2sys are doing on one Virgil.

    A follower's clock source is shown as its leader's name, which every
    Virgil reports, falling back to matching the address.
    """
    if not clock:
        return []
    lines = []
    source = clock.get("source")
    if source is not None and "system_offset_ns" in clock:
        if source.startswith("PHC"):
            follows = "PHC"
        else:
            follows = leader or names.get(source, source)
        parts = [_duration(clock["system_offset_ns"])]
        if "last_offset_ns" in clock:
            parts.append(f"last sample {_duration(clock['last_offset_ns'])}")
        if "rms_offset_ns" in clock:
            parts.append(f"RMS {_duration(abs(clock['rms_offset_ns'])).lstrip('+')}")
        if "frequency_ppm" in clock:
            frequency = clock["frequency_ppm"]
            parts.append(f"{abs(frequency):.3f} ppm {'fast' if frequency >= 0 else 'slow'}")
        lines.append(f"System clock: following {follows}: " + ", ".join(parts))
        if clock.get("synchronised") is False:
            lines.append("System clock: not synchronised yet")
    if "phc_offset_ns" in clock:
        state = "locked" if clock.get("phc_locked") else "settling"
        lines.append(f"PHC: {_duration(clock['phc_offset_ns'])} from system clock ({state}), "
                     f"adjusted {clock.get('phc_adjustment_ppm', 0):+.3f} ppm")
    elif "phc_trim_ppm" in clock:
        lines.append(f"PHC: running free: trim {clock['phc_trim_ppm']:.3f} ppm")
    return lines


QUIET_AFTER = 5


def _alert_line(alert: dict) -> str:
    since = _clock_time(alert.get("since"))
    detail = f" ({alert['summary']})" if alert.get("summary") else ""
    return f"{alert['label']} on {alert['device']}{f' since {since}' if since else ''}: {alert['title']}{detail}"


def _clock_time(value) -> str:
    """A time of day, from netaudio's ISO timestamps or from Unix time."""
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError, OSError):
        return ""
    return moment.strftime("%H:%M:%S")


def cmd_events(args: argparse.Namespace) -> int:
    alerts = fetch_status(args.tls_dir).get("alerts")
    if alerts is None:
        print("This Virgil isn't watching for alerts (netaudio isn't running here).")
        return 0
    if alerts.get("supported") is False:
        print("This Virgil's netaudio doesn't report issues; alerts need netaudio 2.0.0 or later.")
        return 0
    events = alerts.get("recent") or []
    if not events:
        print("No alerts since virgild started. The full record is in /var/lib/dante-virgil/events.jsonl.")
        return 0
    for event in events:
        lasted = f" (lasted {event['lasted_s']:.0f} s)" if event.get("lasted_s") is not None else ""
        detail = f" ({event['summary']})" if event.get("summary") else ""
        print(f"{_clock_time(event['t'])}  {event['event']:8s}  {event['label']} on {event['device']}: "
              f"{event['title']}{detail}{lasted}")
    return 0


def _describe(state: str | None, leader: str | None) -> str:
    if state is None:
        return "not heard from"
    return f"{state} of {leader}" if leader else state


def cmd_status(args: argparse.Namespace) -> int:
    status = fetch_status(args.tls_dir)
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    observing = "  (observing only: roles are still set by hand)" if status.get("observing") else ""
    if status.get("waiting_for_network"):
        observing += "  (waiting for the management network before electing)"
    print(f"{status['name']} (this Virgil): {_describe(status['state'], status['leader'])}{observing}")
    # Alerts come first: a lost lock or a mute matters more than anything else here.
    for alert in (status.get("alerts") or {}).get("open") or []:
        print(f"  ALERT: {_alert_line(alert)}")
    for member in (status.get("group") or {}).get("members", []):
        for alert in member.get("alerts") or []:
            print(f"  ALERT from {member['name']}: {_alert_line(alert)}")
    # Member addresses, so a follower's clock source can be shown by name.
    names = {address: "this Virgil" for address in status.get("addresses") or []}
    for member in (status.get("group") or {}).get("members", []):
        for address in member.get("addresses") or []:
            names[address] = member["name"]
    for line in _describe_clock(status.get("clock"), names, status.get("leader")):
        print(f"  {line}")
    if status.get("ntp_hwtimestamp"):
        print(f"  NTP: hardware timestamps on {status['ntp_hwtimestamp']}")
    if status.get("state") == "leader":
        line = _auto_trim_line(status)
        if line:
            print(f"  {line}")
    ptp = status.get("ptp") or {}
    netaudio = status.get("netaudio")
    if ptp.get("interface") and ptp.get("link_up") is False:
        print(f"  PTP interface {ptp['interface']} has no link")
    elif ptp.get("interface") and ptp.get("has_address") is False:
        print(f"  PTP interface {ptp['interface']} has no IPv4 address")
    elif ptp.get("watching"):
        identity = ptp.get("identity") or "not announced yet"
        print(f"  PTP grandmaster: {identity}")
        dante = {d["address"]: d for d in status.get("dante_devices") or []}
        devices = ptp.get("local_devices") or []
        if not devices:
            print("  Local Dante devices: none heard yet")
        for device in devices:
            print("  " + _describe_device(dante.get(device["address"]), device))
    if netaudio is not None and netaudio.get("running", True) and netaudio.get("reachable") is False:
        print("  WARNING: can't reach netaudio's daemon (systemctl status dante-virgil-netaudio)")
    for warning in ptp.get("warnings") or []:
        print(f"  WARNING: {warning}")
    for device in status.get("dante_devices") or []:
        for warning in device.get("warnings") or []:
            print(f"  WARNING: {warning}")
    group = status.get("group")
    if group is None:
        print("Not in a group")
    else:
        print(f"Group {group['id']}")
        for member in group["members"]:
            line = f"  {member['name']}: {_describe(member['state'], member['leader'])}"
            # Only worth mentioning once a member has gone quiet.
            if member["heard_ago"] is not None and member["heard_ago"] > QUIET_AFTER:
                line += f", NOT HEARD FOR {member['heard_ago']:.0f} s"
            if member["addresses"]:
                line += f", at {', '.join(member['addresses'])}"
            else:
                line += ", address unknown"
            print(line)
            leader = member.get("leader")
            if leader is not None and leader == status.get("name"):
                leader = "this Virgil"
            for line in _describe_clock(member.get("clock"), names, leader):
                print(f"    {line}")
            for device in member.get("dante_devices") or []:
                print("    " + _describe_device(device, None, member["name"]))
            for warning in member.get("ptp_warnings") or []:
                print(f"    WARNING from {member['name']}: {warning}")
            for device in member.get("dante_devices") or []:
                for warning in device.get("warnings") or []:
                    print(f"    WARNING from {member['name']}: {warning}")
    others = status.get("other_virgils") or []
    if others:
        print("Other Virgils on the network:")
        for other in others:
            where = "no group" if other["group_id"] is None else f"group {other['group_id'][:8]}"
            print(f"  {other['name']} ({where}), at {', '.join(other['addresses'])}")
    return 0


# password


def cmd_password(args: argparse.Namespace) -> int:
    import getpass

    from virgil.webauth import MIN_PASSWORD, hash_password

    config = config_file.load(args.config)
    if args.clear:
        config.web_password = None
        config_file.save(config, args.config)
        print("Cleared the web password. The web UI is read-only until a new one is set.")
        return 0
    first = getpass.getpass("New web password: ")
    if len(first) < MIN_PASSWORD:
        raise CliError(f"the password must be at least {MIN_PASSWORD} characters")
    if getpass.getpass("Again: ") != first:
        raise CliError("the passwords didn't match")
    config.web_password = hash_password(first)
    config_file.save(config, args.config)
    print("Set the web password. Anyone already logged in to the web UI will need to log in again.")
    return 0


# trim


def _ago(when: float) -> str:
    seconds = max(0, time.time() - when)
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _auto_trim_line(status: dict) -> str | None:
    auto = status.get("auto_trim")
    if auto is None:
        return None  # an older virgild
    if not auto.get("enabled"):
        return "Auto-trim: off"
    if auto.get("paused"):
        since = f" ({_ago(auto['paused_at'])}; resumes within 10 minutes)" if auto.get("paused_at") else ""
        return f"Auto-trim: PAUSED, because {auto['paused']}{since}"
    last = auto.get("last")
    if not last:
        return "Auto-trim: on, no changes yet" if status.get("state") == "leader" else None
    return (f"Auto-trim: on, last {last['from_ppb'] / 1000:+.3f} to {last['to_ppb'] / 1000:+.3f} ppm "
            f"(consoles {last['consoles_ppb'] / 1000:+.3f} ppm), {_ago(last['at'])}")


def cmd_trim(args: argparse.Namespace) -> int:
    if args.ppm is None:
        status = fetch_status(args.tls_dir)
        leading = status.get("state") == "leader"
        print(f"{status['name']}: trim {status.get('trim_ppb', 0) / 1000:+.3f} ppm, "
              + ("leading" if leading else "not leading, so not in use"))
        line = _auto_trim_line(status)
        if line:
            print(f"  {line}")
        consoles = [(status["name"], d) for d in status.get("dante_devices") or []]
        for member in (status.get("group") or {}).get("members", []):
            consoles += [(member["name"], d) for d in member.get("dante_devices") or []]
        for virgil, device in consoles:
            if device.get("clock_offset_ppb") is not None:
                print(f"  {device['name']} (on {virgil}): clock {device['clock_offset_ppb'] / 1000:+.3f} ppm")
        return 0

    config = config_file.load(args.config)
    try:
        status = fetch_status(args.tls_dir)
    except CliError:
        status = None
    leading = status is not None and status.get("state") == "leader"

    try:
        trim = round(float(args.ppm) * 1000)
    except ValueError as error:
        raise CliError("the trim must be a number of ppm, such as +2.5") from error
    if abs(trim) > 1_000_000:
        raise CliError("the trim must be within ±1000 ppm")
    config.trim_ppb = trim
    config_file.save(config, args.config)
    if leading:
        print(f"{config.name} is leading, so its PHC changes to {trim / 1000:+.3f} ppm within a few seconds.")
    else:
        print(f"Set {config.name}'s trim to {trim / 1000:+.3f} ppm. It only takes effect while it leads.")
    if config.auto_trim:
        print("Auto-trim is on, so it will carry on adjusting from here. "
              "To stop it: sudo virgilctl config set auto_trim false")
    return 0


# device


def _find_device_mac(who: str, args: argparse.Namespace) -> tuple[str, str]:
    """A local Dante device's MAC address and name, from its MAC, address or name."""
    from virgil.consoles import normalise_mac

    mac = normalise_mac(who) if len(who.replace(":", "").replace("-", "")) == 12 else None
    try:
        devices = fetch_status(args.tls_dir).get("dante_devices") or []
    except CliError:
        if mac is not None:
            return mac, mac
        raise
    for device in devices:
        if who in (device.get("name"), device.get("address")) or (mac and device.get("mac") == mac):
            if not device.get("mac"):
                raise CliError(f"netaudio doesn't know {device.get('name')}'s MAC address yet")
            return device["mac"], device.get("name") or device["mac"]
    if mac is not None:
        return mac, mac
    names = ", ".join(d.get("name", "?") for d in devices) or "none"
    raise CliError(f"no local Dante device called {who} (local devices: {names})")


def cmd_device(args: argparse.Namespace) -> int:
    from virgil.config import DeviceSettings

    config = config_file.load(args.config)
    mac, name = _find_device_mac(args.device, args)
    current = config.devices.get(mac, DeviceSettings())
    if args.value is None:
        print(f"{name}: enforce native PTPv2 {'on' if current.native_ptpv2 else 'off'}, "
              f"enforce AES67 {'on' if current.aes67 else 'off'}")
        return 0
    enabled = args.value == "on"
    if args.setting == "ptpv2":
        current.native_ptpv2 = enabled
    else:
        current.aes67 = enabled
    config.devices[mac] = current
    config_file.save(config, args.config)
    label = "native PTPv2" if args.setting == "ptpv2" else "AES67 mode"
    if enabled:
        print(f"Virgil will enforce {name}'s {label}, turning it back on whenever it's turned off.")
    else:
        print(f"Virgil will no longer enforce {name}'s {label}. It won't turn it off, either.")
    if not current.native_ptpv2 and not current.aes67:
        print(f"Note: {name} needs PTPv2, from native PTPv2 or AES67 mode, to follow a Virgil.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virgilctl", description="Control Virgil.")
    parser.add_argument("--version", action="version", version=f"Virgil {__version__}")
    parser.add_argument("--config", type=Path, default=config_file.DEFAULT_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--tls-dir", type=Path, default=tls.TLS_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--control", type=Path, default=CONTROL_SOCKET, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    events = commands.add_parser("events", help="list recent alerts: lost locks, mutes and audio problems")
    events.set_defaults(func=cmd_events)

    status = commands.add_parser("status", help="show this Virgil, its group, and other Virgils it can see")
    status.add_argument("--json", action="store_true", help="print JSON instead of text")
    status.set_defaults(func=cmd_status)

    interfaces = commands.add_parser("interfaces", help="list network interfaces and their PTP support")
    interfaces.add_argument("--json", action="store_true", help="print JSON instead of a table")
    interfaces.set_defaults(func=cmd_interfaces)

    config = commands.add_parser("config", help="show or change this Virgil's settings")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("init", help="create the config file").set_defaults(func=cmd_config_init)
    config_commands.add_parser("show", help="print the config file").set_defaults(func=cmd_config_show)
    config_set = config_commands.add_parser("set", help="change a setting")
    config_set.add_argument("key", choices=["name", "ptp_interface", "mgmt_interface", "trim_ppb", "observe_only",
                                            "auto_trim", "ntp_hwtimestamp"])
    config_set.add_argument("value")
    config_set.add_argument("--yes", action="store_true", help="accept warnings and NIC profiles without asking")
    config_set.set_defaults(func=cmd_config_set)

    role = commands.add_parser("role", help="show or stop this Virgil's role, or set it by hand with observe_only")
    role_commands = role.add_subparsers(dest="role", required=True)
    leader = role_commands.add_parser("leader", help="lead: run free and serve the time")
    leader.add_argument("--allow", action="append", required=True, metavar="ADDRESS",
                        help="an address or network to serve NTP to (repeatable)")
    follower = role_commands.add_parser("follower", help="follow a leader")
    follower.add_argument("--leader", required=True, metavar="ADDRESS", help="the leader's address")
    follower.add_argument("--allow", action="append", metavar="ADDRESS",
                          help="an address or network to serve NTP to (default: the leader)")
    role_commands.add_parser("lead", help="ask the current leader to hand leadership over to this Virgil")
    role_commands.add_parser("stop", help="stop the timing services")
    role_commands.add_parser("show", help="show the current role")
    role.set_defaults(func=cmd_role)

    password = commands.add_parser("password", help="set the web UI's password, which is needed to change things")
    password.add_argument("--clear", action="store_true", help="remove it, making the web UI read-only")
    password.set_defaults(func=cmd_password)

    trim = commands.add_parser("trim", help="show or set this Virgil's PHC trim, in ppm")
    trim.add_argument("ppm", nargs="?", help="the new trim, such as +2.5 or -1")
    trim.set_defaults(func=cmd_trim)

    device = commands.add_parser("device", help="enforce native PTPv2 or AES67 on a local Dante device")
    device.add_argument("device", help="the device's name, address or MAC address")
    device.add_argument("setting", nargs="?", choices=["ptpv2", "aes67"], help="which toggle")
    device.add_argument("value", nargs="?", choices=["on", "off"], help="enforce it, or stop enforcing it")
    device.set_defaults(func=cmd_device)

    tls_parser = commands.add_parser("tls", help="this Virgil's TLS certificate")
    tls_commands = tls_parser.add_subparsers(dest="tls_command", required=True)
    tls_commands.add_parser("init", help="create the certificate, if there isn't one").set_defaults(func=cmd_tls_init)

    group = commands.add_parser("group", help="create, join or show the group")
    group_commands = group.add_subparsers(dest="group_command", required=True)
    group_commands.add_parser("create", help="start a new group with this Virgil").set_defaults(func=cmd_group_create)
    invite = group_commands.add_parser("invite", help="make a one-time invite token for another Virgil")
    invite.add_argument("--minutes", type=int, default=60, help="how long the token works for (default 60)")
    invite.set_defaults(func=cmd_group_invite)
    join = group_commands.add_parser("join", help="join a group with an invite token from one of its members")
    join.add_argument("token")
    join.set_defaults(func=cmd_group_join)
    group_commands.add_parser("show", help="show the group's members").set_defaults(func=cmd_group_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as error:
        if Path(error.filename or "") == args.config:
            print(f"virgilctl: no config file at {args.config}. Run 'virgilctl config init' first.", file=sys.stderr)
        else:
            print(f"virgilctl: {error}", file=sys.stderr)
        return 1
    except PermissionError as error:
        what = error.filename or "a file it needs"
        print(f"virgilctl: permission denied reading {what}. Try again with sudo.", file=sys.stderr)
        return 1
    except (CliError, ConfigError, GroupError, RequestError, ServiceError, UnsafeFileError, tls.TlsError) as error:
        print(f"virgilctl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
