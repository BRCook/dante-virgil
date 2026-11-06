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
from virgil.interfaces import Interface, Timestamping, get_interface, ipv4_addresses, list_interfaces
from virgil.request import REQUEST_PATH, RequestError, TimingRequest, from_json, to_json
from virgil.roles import CHRONY, PHC2SYS, PTP4L, ServiceError, Systemd, apply, current_request, write_request

SOFTWARE_WARNING = (
    "Software timestamping does not work. It is not stable enough for "
    "production use. You can continue if you insist, but you're on your own."
)


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
    interface = get_interface(name)
    if interface is None:
        raise CliError(f"there's no interface called {name}")
    if not interface.physical or interface.mode is Timestamping.NONE:
        raise CliError(f"{name} can't be used for PTP")

    if interface.mode is Timestamping.SOFTWARE:
        print(f"{name} only supports software timestamping.")
        print(SOFTWARE_WARNING)
        if not confirm("Use it anyway?", default=False, assume_yes=assume_yes):
            raise CliError("not changed")
        return name, None

    profile = interface.profile
    if profile is None:
        print(f"{name} uses the {interface.driver} driver, which hasn't been tested. It will use Virgil's defaults.")
        return name, None
    if not profile.needs_hwstamp_ctl:
        return name, interface.driver

    print(f"{name} is a {profile.description}, which is known to need different settings from Virgil's defaults:")
    print(f"  {profile.notes}")
    if confirm("Apply these settings instead of the defaults?", default=True, assume_yes=assume_yes):
        return name, interface.driver
    print("Using the defaults. This NIC will probably not work with them.")
    return name, None


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
    elif key == "observe_only":
        if value.lower() not in ("true", "false"):
            raise CliError("observe_only must be true or false")
        config.observe_only = value.lower() == "true"
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
    group = status.get("group")
    if group is None:
        print("Not in a group")
    else:
        print(f"Group {group['id']}")
        for member in group["members"]:
            line = f"  {member['name']}: {_describe(member['state'], member['leader'])}"
            if member["heard_ago"] is not None:
                line += f", heard {member['heard_ago']} s ago"
            if member["addresses"]:
                line += f", at {', '.join(member['addresses'])}"
            else:
                line += ", address unknown"
            print(line)
    others = status.get("other_virgils") or []
    if others:
        print("Other Virgils on the network:")
        for other in others:
            where = "no group" if other["group_id"] is None else f"group {other['group_id'][:8]}"
            print(f"  {other['name']} ({where}), at {', '.join(other['addresses'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virgilctl", description="Control Virgil.")
    parser.add_argument("--version", action="version", version=f"Virgil {__version__}")
    parser.add_argument("--config", type=Path, default=config_file.DEFAULT_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--tls-dir", type=Path, default=tls.TLS_DIR, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

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
    config_set.add_argument("key", choices=["name", "ptp_interface", "mgmt_interface", "trim_ppb", "observe_only"])
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
    role_commands.add_parser("stop", help="stop the timing services")
    role_commands.add_parser("show", help="show the current role")
    role.set_defaults(func=cmd_role)

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
