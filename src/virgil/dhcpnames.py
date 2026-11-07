"""Per-interface DHCP host names, in ifupdown's /etc/network/interfaces.

Each chosen interface asks the DHCP server for its own name, so a router's
lease list shows which is which: <hostname>-ptp and <hostname>-mgmt. ifupdown
passes a stanza's `hostname` option to the DHCP client (dhcpcd here).

Virgil only touches `iface <name> inet dhcp` stanzas, marks each line it adds
with a comment so it can update or remove it later, and never touches a
`hostname` line someone wrote themselves. The new name applies the next time
the interface comes up: restarting an interface would drop its address, and
on the PTP interface that would interrupt the console's clock.
"""

from __future__ import annotations

import re

MARKER = "# Set by Virgil"
_IFACE = re.compile(r"^iface\s+(\S+)\s+inet\s+(\S+)")
_STANZA_START = re.compile(r"^(iface|auto|allow-\S+|mapping|source|source-directory|rename)\b")
_LABEL = re.compile(r"[^a-z0-9-]+")


def host_label(hostname: str, suffix: str) -> str:
    """A valid DNS label: the host's first label, lower case, with the suffix, at most 63 characters."""
    base = _LABEL.sub("-", hostname.split(".")[0].lower()).strip("-") or "virgil"
    suffix = f"-{suffix}"
    return base[: 63 - len(suffix)].rstrip("-") + suffix


def wanted_names(hostname: str, ptp: str | None, mgmt: str | None) -> dict[str, str]:
    """Interface to DHCP host name. One interface used for both gets the management name."""
    names = {}
    if ptp:
        names[ptp] = host_label(hostname, "ptp")
    if mgmt:
        names[mgmt] = host_label(hostname, "mgmt")
    return names


def apply(text: str, names: dict[str, str]) -> tuple[str, list[str]]:
    """Set Virgil's hostname lines in an interfaces file. Returns the new text and notes about what happened."""
    lines = text.splitlines()
    out: list[str] = []
    notes: list[str] = []
    done: set[str] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _IFACE.match(line)
        if not match:
            out.append(line)
            i += 1
            continue
        interface, method = match.groups()
        out.append(line)
        i += 1
        # The stanza's option lines, up to the next stanza or the end.
        body: list[str] = []
        while i < len(lines) and not _STANZA_START.match(lines[i]):
            body.append(lines[i])
            i += 1
        # Drop Virgil's own lines from before.
        kept: list[str] = []
        skip_next = False
        for option in body:
            if skip_next:
                skip_next = False
                if option.strip().startswith("hostname "):
                    continue
            if option.strip() == MARKER:
                skip_next = True
                continue
            kept.append(option)
        name = names.get(interface)
        if name is not None and interface not in done:
            done.add(interface)
            if method != "dhcp":
                notes.append(f"{interface} doesn't use DHCP, so it has no lease to name")
            elif any(option.strip().startswith("hostname ") for option in kept):
                notes.append(f"{interface} already has a hostname of its own; leaving it")
            else:
                # Options go before any blank lines that end the stanza.
                trailing = 0
                while kept and not kept[-1].strip():
                    kept.pop()
                    trailing += 1
                indent = next((o[: len(o) - len(o.lstrip())] for o in kept if o.strip()), "    ")
                kept += [f"{indent}{MARKER}", f"{indent}hostname {name}"] + [""] * trailing
                notes.append(f"{interface} will ask for the name {name}")
        out.extend(kept)
    for interface in names.keys() - done:
        notes.append(f"{interface} isn't configured in this file, so its lease can't be named here")
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), notes

