"""virgilctl: Virgil's command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

from virgil import __version__
from virgil.interfaces import Interface, Timestamping, list_interfaces

SOFTWARE_WARNING = (
    "Software timestamping does not work. It is not stable enough for "
    "production use. You can continue if you insist, but you're on your own."
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virgilctl", description="Control Virgil.")
    parser.add_argument("--version", action="version", version=f"Virgil {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    interfaces = commands.add_parser("interfaces", help="list network interfaces and their PTP support")
    interfaces.add_argument("--json", action="store_true", help="print JSON instead of a table")
    interfaces.set_defaults(func=cmd_interfaces)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
