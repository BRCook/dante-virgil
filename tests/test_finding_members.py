"""Finding members without trusting adverts (item 7 of the 2.0 plan)."""

import asyncio
import json
import time

from tests.test_daemon import make_pair, start
from virgil import discovery
from virgil.daemon import Daemon


def _advert(daemon, port, address="127.0.0.1"):
    c = daemon.config
    return discovery.Advert(c.id, c.name, "2.0.0", c.group.id, daemon.public_port, port, (address,))


def test_forged_adverts_cant_keep_members_apart_and_contacts_survive_a_restart(tmp_path):
    paths, identities, configs = make_pair(tmp_path)

    async def scenario():
        a, b = Daemon(paths["A"], identities["A"]), Daemon(paths["B"], identities["B"])
        a.contacts_path = tmp_path / "A" / "contacts.json"
        runners = await start(a) + await start(b)
        try:
            a.addresses = ("127.0.0.1",)
            a.on_advert(_advert(b, b.group_port))  # the genuine advert
            for _ in range(5):  # then someone advertises B's ID, again and again, with a wrong port
                a.on_advert(_advert(b, 1))
            assert a.adverts[b.config.id].group_port == 1  # the latest heard is the forgery...
            assert (("127.0.0.1", b.group_port) in a.group_targets(b.config.id))  # ...but the genuine one's kept
            session = await a.group_session()
            await a._send(session, b.config.id, a.heartbeat().encode())
            assert a.config.id in b.peers  # B heard A's heartbeat, despite the forgery
            assert a.contacts[b.config.id] == ("127.0.0.1", b.group_port)
            await a.close_group_session()

            # A restarts, and hears only the forger's advert: the saved contact still comes first.
            restarted = Daemon(paths["A"], identities["A"])
            restarted.contacts_path = a.contacts_path
            restarted.contacts = restarted.load_contacts()
            restarted.on_advert(_advert(b, 1))
            return restarted.group_targets(b.config.id)[0], json.loads(a.contacts_path.read_text())
        finally:
            for runner in runners:
                await runner.cleanup()

    first, saved = asyncio.run(scenario())
    assert first == ("127.0.0.1", saved[configs["B"].id][1])


def test_saved_contacts_are_checked_and_only_ever_written_where_virgil_lives(tmp_path):
    paths, identities, configs = make_pair(tmp_path)
    a = Daemon(paths["A"], identities["A"])
    a.contacts_path = tmp_path / "missing" / "contacts.json"
    a.remember_contact(configs["B"].id, "192.0.2.7", 8741)
    assert not (tmp_path / "missing").exists()  # remembered, but no folder created
    a.contacts_path = tmp_path / "contacts.json"
    a.contacts_path.write_text(json.dumps({configs["B"].id: ["192.0.2.7", 8741], "x": ["1.2.3.4", 99999],
                                           "y": "nonsense"}))
    assert a.load_contacts() == {configs["B"].id: ("192.0.2.7", 8741)}
