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
