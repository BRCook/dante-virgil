"""Choosing interfaces: the rules virgilctl and the web UI both follow.

Some choices need a person to confirm something first: accepting a NIC's
profile when its driver is known to need different settings, or going ahead
with software timestamping despite the warning. check_ptp_interface says when
that's needed, and the caller asks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from virgil.interfaces import Interface, Timestamping, get_interface, list_interfaces

SOFTWARE_WARNING = (
    "Software timestamping does not work. It is not stable enough for "
    "production use. You can continue if you insist, but you're on your own."
)


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class PtpChoice:
    name: str
    nic_profile: str | None
    # "profile" or "software" when someone has to confirm before this is used.
    needs: str | None = None
    message: str | None = None
    # Said once the choice is made, such as that a driver is untested.
    note: str | None = None


def check_ptp_interface(
    name: str,
    accept_profile: bool | None = None,
    accept_software: bool = False,
    lookup: Callable[[str], Interface | None] | None = None,
) -> PtpChoice:
    """Check a PTP interface. accept_profile None means it hasn't been asked yet."""
    interface = (lookup or get_interface)(name)
    if interface is None:
        raise SetupError(f"there's no interface called {name}")
    if not interface.physical or interface.mode is Timestamping.NONE:
        raise SetupError(f"{name} can't be used for PTP")

    if interface.mode is Timestamping.SOFTWARE:
        if not accept_software:
            return PtpChoice(name, None, "software", f"{name} only supports software timestamping. {SOFTWARE_WARNING}")
        return PtpChoice(name, None)

    profile = interface.profile
    if profile is None:
        return PtpChoice(name, None, note=f"{name} uses the {interface.driver} driver, which hasn't been tested. "
                                          "It will use Virgil's defaults.")
    if not profile.needs_hwstamp_ctl:
        return PtpChoice(name, interface.driver)
    if accept_profile is None:
        return PtpChoice(name, None, "profile",
                         f"{name} is a {profile.description}, which is known to need different settings from "
                         f"Virgil's defaults: {profile.notes} Apply these settings instead of the defaults?")
    if accept_profile:
        return PtpChoice(name, interface.driver)
    return PtpChoice(name, None, note="Using the defaults. This NIC will probably not work with them.")


def check_mgmt_interface(name: str, lookup: Callable[[str], Interface | None] | None = None) -> str:
    if (lookup or get_interface)(name) is None:
        raise SetupError(f"there's no interface called {name}")
    return name


INTERFACES_FILE = "/etc/network/interfaces"


def configured(interfaces_file: str = INTERFACES_FILE) -> set[str] | None:
    """Interfaces ifupdown is set up to bring up, or None if ifupdown isn't in use."""
    from virgil.dhcpnames import configured_interfaces, interfaces_texts

    texts = interfaces_texts(interfaces_file)
    return None if texts is None else configured_interfaces(texts)


def describe_interfaces(interfaces: list[Interface] | None = None,
                        set_up: set[str] | None | bool = True) -> list[dict[str, Any]]:
    """Every interface, as the web UI shows them."""
    known = configured() if set_up is True else set_up
    result = []
    for interface in interfaces if interfaces is not None else list_interfaces():
        profile = interface.profile
        result.append({
            "name": interface.name,
            # The real name first; the NIC profile's for NICs the database doesn't know yet.
            "nic": interface.hardware_name or (profile.description if profile else None),
            "driver": interface.driver,
            "link_up": interface.link_up,
            "physical": interface.physical,
            "timestamping": interface.mode.value if interface.physical else "virtual",
            "ptp_capable": interface.ptp_capable,
            # False when ifupdown won't bring it up; None when ifupdown isn't in use.
            "configured": None if known is None else interface.name in known,
            "profile": None if profile is None else {
                "description": profile.description,
                "status": profile.status.value,
                "notes": profile.notes,
            },
        })
    return result
