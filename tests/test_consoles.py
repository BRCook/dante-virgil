import asyncio

from aiohttp import web

from virgil.config import DeviceSettings
from virgil.consoles import Consoles, NetaudioClient, normalise_mac, parse_devices

WING = {
    "name": "Wing-Compact",
    "server_name": "Wing-Compact.local.",
    "ipv4": "10.10.100.50",
    "mac_address": "001DC1AABBCC",
    "product_name": "WING Compact",
    "aes67_current": False,
    "aes67_configured": False,
    "clock_status": {"ptpv2_enabled": False},
    "clock_frequency_offset_parts_per_billion": 27115,
}
FAR = {**WING, "name": "Wing-Rack", "ipv4": "10.10.100.60", "mac_address": "00:1d:c1:aa:bb:dd"}


def test_normalise_mac():
    assert normalise_mac("001DC1AABBCC") == "00:1d:c1:aa:bb:cc"
    assert normalise_mac("00-1d-c1-aa-bb-cc") == "00:1d:c1:aa:bb:cc"
    assert normalise_mac("nope") is None
    assert normalise_mac(None) is None


def test_parse_devices():
    (device,) = parse_devices({"Wing-Compact.local.": WING, "broken": {"ipv4": "None"}, "junk": 5})
    assert device.name == "Wing-Compact" and device.mac == "00:1d:c1:aa:bb:cc"
    assert device.model == "WING Compact" and device.ptpv2 is False and device.clock_offset_ppb == 27115
    assert parse_devices([]) == []


class FakeNetaudio:
    def __init__(self, devices):
        self.devices = devices
        self.changes = []

    def app(self):
        app = web.Application()
        app.router.add_get("/devices", self.list_devices)
        app.router.add_get("/diagnostics/{device}", self.diagnostics)
        app.router.add_post("/set-clock-configuration", self.set_clock)
        app.router.add_post("/set-aes67", self.set_aes67)
        return app

    async def list_devices(self, request):
        return web.json_response(self.devices)

    async def diagnostics(self, request):
        return web.json_response({"device": request.match_info["device"], "clock": {}})

    async def set_clock(self, request):
        body = await request.json()
        self.changes.append(("clock", body))
        record = next(r for r in self.devices.values() if r["name"] == body["device"].removesuffix(".local."))
        record["clock_status"]["ptpv2_enabled"] = body["changes"]["ptpv2_enabled"]
        return web.json_response({"ok": True})

    async def set_aes67(self, request):
        body = await request.json()
        self.changes.append(("aes67", body))
        return web.json_response({"ok": True})


async def _with_fake(fake, scenario):
    runner = web.AppRunner(fake.app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        return await scenario(NetaudioClient(f"http://127.0.0.1:{port}"))
    finally:
        await runner.cleanup()


def _devices():
    return {"Wing-Compact.local.": {**WING, "clock_status": {"ptpv2_enabled": False}},
            "Wing-Rack.local.": {**FAR, "clock_status": {"ptpv2_enabled": True}}}


def test_only_local_devices_are_reported():
    fake = FakeNetaudio(_devices())
    now = [100.0]

    async def scenario(client):
        consoles = Consoles(client, lambda: {"10.10.100.50": False}, lambda: {}, clock=lambda: now[0])
        await consoles.poll()
        return consoles

    consoles = asyncio.run(_with_fake(fake, scenario))
    assert consoles.reachable is True
    assert [d["name"] for d in consoles.summaries()] == ["Wing-Compact"]
    assert consoles.summaries()[0]["warnings"] == ["Wing-Compact has PTPv2 off, so it can't follow this Virgil"]
    assert "10.10.100.50" in consoles.diagnostics
    assert fake.changes == []  # No toggle is on, so nothing is changed.


def test_a_toggled_setting_is_kept_on():
    fake = FakeNetaudio(_devices())
    settings = {"00:1d:c1:aa:bb:cc": DeviceSettings(native_ptpv2=True)}
    now = [100.0]

    async def scenario(client):
        consoles = Consoles(client, lambda: {"10.10.100.50": True}, lambda: settings, clock=lambda: now[0])
        await consoles.poll()
        now[0] += 10
        await consoles.poll()
        # The console reboots and forgets PTPv2; Virgil turns it back on.
        fake.devices["Wing-Compact.local."]["clock_status"]["ptpv2_enabled"] = False
        now[0] += 10
        await consoles.poll()  # within 30 s of the last attempt: wait
        now[0] += 30
        await consoles.poll()
        return consoles

    asyncio.run(_with_fake(fake, scenario))
    assert [body["changes"] for kind, body in fake.changes] == [{"ptpv2_enabled": True}] * 2
    assert all(body["device"] == "Wing-Compact.local." for _, body in fake.changes)


def test_aes67_toggle():
    fake = FakeNetaudio(_devices())
    settings = {"00:1d:c1:aa:bb:cc": DeviceSettings(aes67=True)}

    async def scenario(client):
        consoles = Consoles(client, lambda: {"10.10.100.50": True}, lambda: settings, clock=lambda: 100.0)
        await consoles.poll()

    asyncio.run(_with_fake(fake, scenario))
    assert fake.changes == [("aes67", {"device": "Wing-Compact.local.", "enabled": True})]


def test_ptpv2_on_but_not_following():
    devices = _devices()
    devices["Wing-Compact.local."]["clock_status"]["ptpv2_enabled"] = True
    fake = FakeNetaudio(devices)
    now = [100.0]

    async def scenario(client):
        consoles = Consoles(client, lambda: {"10.10.100.50": False}, lambda: {}, clock=lambda: now[0])
        await consoles.poll()
        first = consoles.summaries()[0]["warnings"]
        now[0] += 25
        await consoles.poll()
        return first, consoles.summaries()[0]["warnings"]

    first, later = asyncio.run(_with_fake(fake, scenario))
    assert first == []  # A grace period first.
    assert later == ["Wing-Compact has PTPv2 on, but isn't following this Virgil"]


def test_aes67_needing_a_restart():
    devices = _devices()
    devices["Wing-Compact.local."].update(aes67_configured=True, aes67_current=False)
    devices["Wing-Compact.local."]["clock_status"]["ptpv2_enabled"] = True
    fake = FakeNetaudio(devices)

    async def scenario(client):
        consoles = Consoles(client, lambda: {"10.10.100.50": True}, lambda: {}, clock=lambda: 100.0)
        await consoles.poll()
        return consoles.summaries()[0]["warnings"]

    assert asyncio.run(_with_fake(fake, scenario)) == [
        "Wing-Compact has AES67 mode set, but needs restarting for it to take effect"
    ]


def test_netaudio_unreachable():
    async def scenario():
        consoles = Consoles(NetaudioClient("http://127.0.0.1:9", timeout=1), lambda: {}, lambda: {})
        await consoles.poll()
        return consoles

    consoles = asyncio.run(scenario())
    assert consoles.reachable is False and consoles.summaries() == []


def test_a_mac_nested_anywhere_in_the_record():
    from virgil.consoles import find_mac

    nested = {**WING, "mac_address": None, "network": {"interfaces": [{"name": "primary", "mac_address": "00:1D:C1:8D:5D:66"}]}}
    del nested["mac_address"]
    (device,) = parse_devices({"WingCompact.local.": nested})
    assert device.mac == "00:1d:c1:8d:5d:66"
    assert find_mac({"a": {"b": {"c": {"d": {"e": {"f": {"mac_address": "001dc1aabbcc"}}}}}}}) is None  # too deep


def test_the_mac_from_a_ptp_clock_identity():
    from virgil.consoles import mac_from_clock_identity

    assert mac_from_clock_identity("00:1d:c1:ff:fe:8d:5d:66") == "00:1d:c1:8d:5d:66"
    assert mac_from_clock_identity("001dc1fffe8d5d66") == "00:1d:c1:8d:5d:66"
    assert mac_from_clock_identity("00:1d:c1:12:34:8d:5d:66") is None  # not made from a MAC
    assert mac_from_clock_identity(None) is None


def test_falling_back_to_the_ptp_clock_identity():
    no_mac = {k: v for k, v in WING.items() if k != "mac_address"}
    fake = FakeNetaudio({"WingCompact.local.": {**no_mac, "clock_status": {"ptpv2_enabled": True}}})

    async def scenario(client):
        consoles = Consoles(client, lambda: {"10.10.100.50": True}, lambda: {}, clock=lambda: 100.0,
                            clock_identity=lambda address: "00:1d:c1:ff:fe:8d:5d:66")
        await consoles.poll()
        return consoles.summaries()[0]["mac"]

    assert asyncio.run(_with_fake(fake, scenario)) == "00:1d:c1:8d:5d:66"


def test_a_mac_given_as_a_clock_identity():
    (device,) = parse_devices({"WingCompact.local.": {**WING, "mac_address": "001dc1fffe8d5d66"}})
    assert device.mac == "00:1d:c1:8d:5d:66"
