"""Telling people a newer Virgil exists.

Once a day (and a minute after starting, and whenever the `check_updates` setting is
turned back on), virgild asks GitHub for Virgil's latest release. If it's newer than
the version running, the web UI and `virgilctl status` say so, with a link to its
release notes. Nothing is downloaded or installed: when to update a Clock Box is
always a person's choice, never Virgil's, since even an update that only restarts
virgild shouldn't arrive in the middle of a show by itself.

Clock Boxes often run without internet access, so a check that can't reach GitHub
just tries again the next day, quietly: it's not an alert. GitHub's "latest release"
leaves out drafts and pre-releases, so only real releases are offered.
"""

from __future__ import annotations

import re
from typing import Any

import aiohttp

LATEST_URL = "https://api.github.com/repos/BRCook/dante-virgil/releases/latest"
RELEASES = "https://github.com/BRCook/dante-virgil/releases/"
FIRST_CHECK = 60.0  # seconds after virgild starts
CHECK_EVERY = 24 * 3600.0
TIMEOUT = 15.0
VERSION = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")


def parse_version(text: Any) -> tuple[int, ...] | None:
    """A release's version, as numbers to compare ("v1.0.1" is (1, 0, 1)); None if it isn't a plain version."""
    found = VERSION.match(text.strip()) if isinstance(text, str) else None
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def newer(latest: str, running: str) -> bool:
    """Whether a release is newer than the version running. Anything that isn't a plain version (a development
    copy's "unknown", say) is never called out of date, rather than nagging about it."""
    theirs, ours = parse_version(latest), parse_version(running)
    if theirs is None or ours is None:
        return False
    width = max(len(theirs), len(ours))
    return theirs + (0,) * (width - len(theirs)) > ours + (0,) * (width - len(ours))


def from_release(data: Any, running: str) -> dict[str, Any]:
    """What a release from GitHub's API means here: its version, a link to its notes (only ever to this project's
    releases on GitHub, since the page shows it as a link), and whether it's newer than the version running."""
    if not isinstance(data, dict):
        raise ValueError("GitHub's reply isn't an object")
    version = parse_version(data.get("tag_name"))
    if version is None:
        raise ValueError(f"the latest release has an unexpected tag: {str(data.get('tag_name'))[:40]!r}")
    latest = ".".join(str(part) for part in version)
    url = data.get("html_url")
    if not isinstance(url, str) or not url.startswith(RELEASES):
        url = RELEASES + "latest"
    return {"latest": latest, "url": url, "available": newer(latest, running)}


async def fetch_latest(running: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    """Ask GitHub for the latest release. Raises if it can't be reached or makes no sense."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"Virgil/{running}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(LATEST_URL, headers=headers) as reply:
            if reply.status != 200:
                raise ValueError(f"GitHub answered {reply.status}")
            return from_release(await reply.json(content_type=None), running)
