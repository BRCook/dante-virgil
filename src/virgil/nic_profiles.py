"""Known network drivers, and how they differ from Virgil's defaults."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProfileStatus(Enum):
    TESTED = "tested"
    EXPECTED = "expected to work"
    UNSUPPORTED = "no hardware timestamping"


@dataclass(frozen=True)
class NicProfile:
    driver: str
    description: str
    status: ProfileStatus
    notes: str
    # Arguments for hwstamp_ctl, for drivers whose PHC only runs once hardware
    # timestamping has been turned on. None means hwstamp_ctl isn't needed.
    hwstamp_ctl_args: tuple[str, ...] | None = None

    @property
    def needs_hwstamp_ctl(self) -> bool:
        return self.hwstamp_ctl_args is not None


PROFILES: dict[str, NicProfile] = {
    profile.driver: profile
    for profile in (
        NicProfile(
            driver="igc",
            description="Intel i225/i226",
            status=ProfileStatus.TESTED,
            notes=(
                "Works with the defaults. The receive filter only supports "
                "'none' and 'all', so use 'hwstamp_ctl -r 1' if it is ever needed."
            ),
        ),
        NicProfile(
            driver="r8126",
            description="Realtek RTL8126",
            status=ProfileStatus.TESTED,
            notes=(
                "Needs the hardware timestamping patch from wireless-dante. "
                "hwstamp_ctl must run before the PHC runs. It has one transmit "
                "timestamp slot, so only one PTP process can use it."
            ),
            # Transmit timestamps on, receive filter HWTSTAMP_FILTER_PTP_V2_EVENT.
            hwstamp_ctl_args=("-t", "1", "-r", "12"),
        ),
        NicProfile(
            driver="igb",
            description="Intel i210",
            status=ProfileStatus.EXPECTED,
            notes="Untested.",
        ),
        NicProfile(
            driver="r8169",
            description="Realtek",
            status=ProfileStatus.UNSUPPORTED,
            notes="No PTP hardware clock. Software timestamping only.",
        ),
    )
}


def profile_for(driver: str | None) -> NicProfile | None:
    """Return the profile for a driver, or None if the driver isn't known."""
    if driver is None:
        return None
    return PROFILES.get(driver)
