"""Stronger password hashes, once every member can check them (item 4 of the 2.0 plan)."""

from tests.test_webapi import HEADERS, PASSWORD, _login, run
from tests.test_webapi import virgil  # noqa: F401 (the fixture)
from virgil import webauth
from virgil.election import State
from virgil.webauth import LEGACY, STRONG, Attempts, check_password, hash_password, weaker_than_strong


def test_strong_and_legacy_hashes():
    legacy, strong = hash_password("a long password"), hash_password("a long password", strong=True)
    assert legacy.startswith(f"scrypt${LEGACY[0]}${LEGACY[1]}${LEGACY[2]}$")
    assert strong.startswith(f"scrypt${STRONG[0]}${STRONG[1]}${STRONG[2]}$")
    assert check_password("a long password", legacy) and check_password("a long password", strong)
    assert not check_password("wrong", strong)
    assert weaker_than_strong(legacy) and not weaker_than_strong(strong) and not weaker_than_strong(None)


def _upgrades(virgil, features, state):
    async def scenario(daemon, session, base):
        daemon.config.features = features
        daemon.election.resume(state, None, 0.0)
        before = daemon.config.web_password
        status = await _login(session, base)
        return status, before, daemon.config.web_password

    return run(virgil, scenario)


def test_a_login_strengthens_a_1x_hash_once_the_group_can_check_it(virgil):
    status, before, after = _upgrades(virgil, ["strong_hashes"], State.LEADER)
    assert status == 200 and weaker_than_strong(before) and not weaker_than_strong(after)
    assert check_password(PASSWORD, after) and after != before


def test_no_strengthening_while_any_member_couldnt_check_it(virgil):
    status, before, after = _upgrades(virgil, [], State.LEADER)  # a 1.x member in the group: no strong_hashes
    assert status == 200 and after == before


def test_no_strengthening_before_the_clock_settles(virgil):
    from virgil import group as groups

    async def scenario(daemon, session, base):
        daemon.config.group = groups.create(daemon.config.id, daemon.config.name, daemon.identity.cert_pem, 1.0)
        daemon.config.features = ["strong_hashes"]
        before = daemon.config.web_password  # in a group, still listening for a leader: not settled
        return await _login(session, base), before, daemon.config.web_password

    status, before, after = run(virgil, scenario)
    assert status == 200 and after == before


def test_the_wrong_password_table_forgets_quiet_addresses():
    now = [0.0]
    attempts = Attempts(clock=lambda: now[0])
    for i in range(500):
        attempts.failed(f"10.0.{i // 250}.{i % 250}")
    assert not attempts.blocked("192.0.2.1")
    assert len(attempts._failures) == 500  # recent: all still remembered, and an innocent address isn't added
    now[0] = webauth.ATTEMPT_WINDOW + 1
    attempts.blocked("192.0.2.1")
    assert attempts._failures == {}


def test_new_passwords_are_longer():
    assert webauth.MIN_PASSWORD == 12


def test_a_synchronised_follower_leaves_re_hashing_to_the_leader(virgil):
    from virgil import group as groups

    async def scenario(daemon, session, base):
        daemon.config.group = groups.create(daemon.config.id, daemon.config.name, daemon.identity.cert_pem, 1.0)
        daemon.config.features = ["strong_hashes"]
        daemon.election.resume(State.FOLLOWER, "b" * 32, 0.0)
        daemon.clock = {"synchronised": True}
        before = daemon.config.web_password
        return await _login(session, base), before, daemon.config.web_password

    status, before, after = run(virgil, scenario)
    assert status == 200 and after == before
