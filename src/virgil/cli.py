"""virgilctl: Virgil's command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from virgil import __version__
from virgil import config as config_file
from virgil.config import ConfigError
from virgil.files import UnsafeFileError
from virgil import group as groups
from virgil import tls
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
    print(f"Created {args.config} for box {config.name} ({config.id})")
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
    config_file.save(config, args.config)
    print(f"Set {key}")
    return 0


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

    new = None if args.role == "stop" else _request_for(Role(args.role), args)
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
        raise CliError("this box is already in a group")
    cert = tls.default_identity(args.tls_dir).cert_pem
    config.group = groups.create(config.id, config.name, cert)
    config_file.save(config, args.config)
    print(f"Created group {config.group.id}, with {config.name} as its first member")
    return 0


def cmd_group_invite(args: argparse.Namespace) -> int:
    config = config_file.load(args.config)
    if config.group is None:
        raise CliError("this box isn't in a group. Create one with 'virgilctl group create'")
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
    print(f"\nThis token can be used once, until {until}. On the new box, run:\n", file=sys.stderr)
    print("  sudo virgilctl group join <token>", file=sys.stderr)
    return 0


def cmd_group_show(args: argparse.Namespace) -> int:
    config = config_file.load(args.config)
    if config.group is None:
        print("Not in a group")
        return 0
    print(f"Group {config.group.id}")
    for box_id, member in sorted(config.group.members.items(), key=lambda item: item[1].name):
        marker = " (this box)" if box_id == config.id else ""
        try:
            code = tls.short_code(tls.fingerprint(member.cert))
        except tls.TlsError:
            code = "bad certificate"
        print(f"  {member.name}{marker}  {box_id}  {code}")
    pending = [e for e in config.group.invites.values() if e > time.time()]
    if pending:
        print(f"{len(pending)} unused invite(s) outstanding")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virgilctl", description="Control Virgil.")
    parser.add_argument("--version", action="version", version=f"Virgil {__version__}")
    parser.add_argument("--config", type=Path, default=config_file.DEFAULT_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--tls-dir", type=Path, default=tls.TLS_DIR, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    interfaces = commands.add_parser("interfaces", help="list network interfaces and their PTP support")
    interfaces.add_argument("--json", action="store_true", help="print JSON instead of a table")
    interfaces.set_defaults(func=cmd_interfaces)

    config = commands.add_parser("config", help="show or change this box's settings")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("init", help="create the config file").set_defaults(func=cmd_config_init)
    config_commands.add_parser("show", help="print the config file").set_defaults(func=cmd_config_show)
    config_set = config_commands.add_parser("set", help="change a setting")
    config_set.add_argument("key", choices=["name", "ptp_interface", "mgmt_interface", "trim_ppb"])
    config_set.add_argument("value")
    config_set.add_argument("--yes", action="store_true", help="accept warnings and NIC profiles without asking")
    config_set.set_defaults(func=cmd_config_set)

    role = commands.add_parser("role", help="set this box's role by hand (until the daemon does it)")
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

    tls_parser = commands.add_parser("tls", help="this box's TLS certificate")
    tls_commands = tls_parser.add_subparsers(dest="tls_command", required=True)
    tls_commands.add_parser("init", help="create the certificate, if there isn't one").set_defaults(func=cmd_tls_init)

    group = commands.add_parser("group", help="create, join or show the group")
    group_commands = group.add_subparsers(dest="group_command", required=True)
    group_commands.add_parser("create", help="start a new group with this box").set_defaults(func=cmd_group_create)
    invite = group_commands.add_parser("invite", help="make a one-time invite token for another box")
    invite.add_argument("--minutes", type=int, default=60, help="how long the token works for (default 60)")
    invite.set_defaults(func=cmd_group_invite)
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
        print(f"virgilctl: can't read {error.filename}. Try again with sudo.", file=sys.stderr)
        return 1
    except (CliError, ConfigError, GroupError, RequestError, ServiceError, UnsafeFileError, tls.TlsError) as error:
        print(f"virgilctl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
