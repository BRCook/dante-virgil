"""Authenticated NTP between members (see the design document's "Authenticated NTP").

Each group has one key, 32 random bytes with a small ID, made when the group switches
authenticated NTP on (see virgil.features). It's a shared setting (see virgil.shared),
so it only travels between members, over their mutual TLS, and new members get it when
they join. Chrony signs and checks every NTP packet between members with it.

The key changes whenever a member is removed, since that member knows the old one. The
new key keeps the old one as its "previous", and both go into chrony's key file, so the
leader goes on accepting a follower still using the old one until it moves over, and
nobody's sync is interrupted.
"""

from __future__ import annotations

import secrets

from virgil.config import NTP_KEY_IDS, NtpKey


def new_key(replacing: NtpKey | None = None) -> NtpKey:
    """A new key, with an ID different from the one it replaces, which it keeps as its previous key."""
    if replacing is None:
        return NtpKey(secrets.randbelow(NTP_KEY_IDS[1]) + NTP_KEY_IDS[0], secrets.token_hex(32))
    next_id = replacing.id % NTP_KEY_IDS[1] + NTP_KEY_IDS[0]
    return NtpKey(next_id, secrets.token_hex(32), replacing.id, replacing.key)
