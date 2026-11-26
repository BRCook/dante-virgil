"""Settings every member shares: checked, adopted when newer, and spread by real heartbeats."""

import asyncio

import pytest

from virgil import config as config_file
from virgil import shared
from virgil.daemon import Daemon
from virgil.webauth import hash_password
from tests.test_daemon import introduce, make_pair, start


def test_a_newer_change_is_adopted_and_an_older_one_isnt():
    config = config_file.new_config()
    assert shared.adopt(config, {"auto_trim": (False, 10.0)}) == [("auto_trim", False)]
    assert config.auto_trim is False and config.auto_trim_changed == 10.0
    assert shared.adopt(config, {"auto_trim": (True, 5.0)}) == []  # older: ignored
    assert shared.adopt(config, {"auto_trim": (True, 10.0)}) == []  # the same moment: ours stands
    assert config.auto_trim is False


def test_shared_settings_are_checked():
    good = {"auto_trim": {"value": True, "changed": 1.0},
            "web_password": {"value": hash_password("correct horse"), "changed": 2.0}}
    assert shared.parse(good)["auto_trim"] == (True, 1.0)
    for bad in ({"auto_trim": {"value": "yes", "changed": 1.0}},
                {"web_password": {"value": "plaintext!", "changed": 1.0}},
                {"auto_trim": {"value": True, "changed": True}}):
        with pytest.raises(shared.SharedError):
            shared.parse(bad)
    assert shared.parse({"web_password": {"value": None, "changed": 3.0}}) == {"web_password": (None, 3.0)}


def test_a_change_on_one_virgil_reaches_the_other(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
        runners = await start(a) + await start(b)
        try:
            introduce(a, b)
            introduce(b, a)
            a.shared_setting_changed_here("auto_trim", False, "from its page")
            a.config.auto_trim = False
            a.config.web_password, a.config.web_password_changed = hash_password("hunter2"), a.config.auto_trim_changed
            await a._member_call(configs["B"].id, "POST", "/group/v1/heartbeat", a.heartbeat().encode())
            return a, b
        finally:
            for runner in runners:
                await runner.cleanup()

    a, b = asyncio.run(scenario())
    assert b.config.auto_trim is False and b.config.web_password == a.config.web_password
    assert config_file.load(paths["B"]).auto_trim is False  # saved
    titles = [e["title"] for e in b.events.recent]
    assert "Virgil A turned automatic trim off for the group" in titles
    assert "Virgil A changed the web UI's password for the group" in titles
    assert "This Virgil turned automatic trim off for the group (from its page)" in [e["title"] for e in a.events.recent]


def test_an_unchanged_heartbeat_saves_nothing(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    b = Daemon(paths["B"], identities["B"])
    saves = []
    b.save = lambda: saves.append(1)
    b.adopt_shared_settings(shared.parse(shared.snapshot(b.config)), configs["A"].id)
    assert saves == []


def test_software_timestamping_is_warned_about_for_as_long_as_it_lasts(tmp_path):
    from virgil.interfaces import Timestamping

    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.config.ptp_interface = "enp5s0"
    a.ptp_timestamping = lambda now: Timestamping.SOFTWARE
    assert any("only has software timestamping" in w for w in a.ptp_warnings(100.0))
    a.ptp_timestamping = lambda now: Timestamping.HARDWARE
    assert not any("software timestamping" in w for w in a.ptp_warnings(100.0))


# A wrong clock mustn't win: see virgil.shared.

def test_which_clocks_have_settled():
    from virgil.shared import clock_settled

    assert clock_settled(False, "listen", False)  # not in a group: nothing to share
    assert clock_settled(True, "leader", False)  # its clock is the group's time
    assert clock_settled(True, "follower", True) and not clock_settled(True, "follower", False)
    assert not any(clock_settled(True, state, True) for state in ("listen", "sync", "stopped", None))


def test_a_change_is_always_stamped_later_than_the_one_it_replaces():
    from virgil.shared import next_stamp

    assert next_stamp(1000.0, 2000.0) == 2000.0
    years_ahead = 2000.0 + 5 * 365 * 86400  # the group's time was ahead, then corrected
    assert next_stamp(years_ahead, 2000.0) == pytest.approx(years_ahead + 0.001)


def test_a_newcomer_takes_on_the_groups_settings_whatever_its_own_claim():
    from virgil.config import new_config
    from virgil.shared import take_on_joining

    now, future = 1_800_000_000.0, 1_800_000_000.0 + 5 * 365 * 86400
    newcomer = new_config()
    newcomer.auto_trim, newcomer.auto_trim_changed = False, future
    newcomer.web_password, newcomer.web_password_changed = "theirs", future
    # The group turned auto-trim on, and never set a password.
    take_on_joining(newcomer, {"auto_trim": (True, now - 100), "web_password": (None, 0.0)}, now)
    assert (newcomer.auto_trim, newcomer.auto_trim_changed) == (True, now - 100)
    # What the group never set stays, but can't claim to be newer than the inviting Virgil's clock.
    assert (newcomer.web_password, newcomer.web_password_changed) == ("theirs", now)

    older = new_config()
    older.auto_trim_changed = future
    take_on_joining(older, None, None)  # an inviting Virgil from before this: the group's settings win
    assert older.auto_trim_changed == 0.0
