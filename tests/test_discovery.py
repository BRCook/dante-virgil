import asyncio

from virgil import discovery
from virgil.discovery import parse

ID = "a" * 32


def test_parse():
    props = {b"id": ID.encode(), b"name": b"Virgil A", b"v": b"0.1.0", b"g": b"", b"pp": b"8740", b"gp": b"8741"}
    advert = parse(props, ["10.0.0.5"])
    assert advert == discovery.Advert(ID, "Virgil A", "0.1.0", None, 8740, 8741, ("10.0.0.5",))


def test_parse_rejects_junk():
    good = {b"id": ID.encode(), b"pp": b"8740", b"gp": b"8741"}
    assert parse({**good, b"id": b"x"}, ["10.0.0.5"]) is None
    assert parse({**good, b"pp": b"port"}, ["10.0.0.5"]) is None
    assert parse(good, []) is None


def test_two_virgils_find_each_other():
    async def scenario():
        found = []
        a = discovery.Discovery(found.append)
        b = discovery.Discovery(lambda advert: None)
        await a.start(["127.0.0.1"], "Virgil on A", "host-a", 18740,
                      discovery.properties("a" * 32, "A", "0.1", None, 18740, 18741))
        await b.start(["127.0.0.1"], "Virgil on B", "host-b", 18750,
                      discovery.properties("b" * 32, "B", "0.1", "c" * 32, 18750, 18751))
        try:
            for _ in range(50):
                await asyncio.sleep(0.2)
                if any(advert.id == "b" * 32 for advert in found):
                    break
        finally:
            await a.stop()
            await b.stop()
        return found

    found = asyncio.run(scenario())
    b = next(advert for advert in found if advert.id == "b" * 32)
    assert b.group_id == "c" * 32 and b.group_port == 18751 and b.addresses == ("127.0.0.1",)
