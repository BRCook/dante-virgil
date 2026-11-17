import struct

import pytest

from virgil import ptpv1
from virgil.ptpwatch import PtpState, mac_of_identity, parse

BOX_A = "8c:a6:82:ff:fe:71:3b:b6"
BOX_A_MAC = "8c:a6:82:71:3b:b6"
WING_UUID = bytes.fromhex("001dc18d5d66")


def frame(source: str, payload: bytes, port: int = 320) -> bytes:
    ip = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 1, 17, 0, 0]) + bytes(map(int, source.split("."))) + bytes([224, 0, 1, 129])
    return bytes(12) + b"\x08\x00" + ip + struct.pack("!HHHH", port, port, 0, 0) + payload


def wing_sync(boundary: bool, grandmaster: bytes) -> bytes:
    """The WingCompact's PTPv1 Sync, as captured: a boundary clock naming Box A as grandmaster."""
    flags = (0x04 if boundary else 0) | 0x08
    return ptpv1.header(ptpv1.SYNC, 63912, WING_UUID, flags) + ptpv1.SYNC_BODY.pack(
        1794861972, 1128400, 0, 0, 1, grandmaster, 1, 1, 15, b"DFLT", 32767, 1, 0, -2, 32767, 1, 15, b"DFLT",
        1, grandmaster, 1, 0, 0, 0)


def test_mac_of_identity():
    assert mac_of_identity(BOX_A) == BOX_A_MAC
    assert mac_of_identity("00:11:22:33:44:55:66:77") is None and mac_of_identity(None) is None


def test_ptpv1_is_parsed():
    packet = parse(frame("192.168.20.224", wing_sync(True, bytes.fromhex(BOX_A_MAC.replace(":", ""))), 319))
    assert packet.version == 1 and packet.v1_control == ptpv1.SYNC
    assert packet.v1_flags & 0x04 and packet.v1_grandmaster == BOX_A_MAC


def test_a_bridging_device_is_recognised():
    state = PtpState(own_identity=BOX_A)
    ours = bytes.fromhex(BOX_A_MAC.replace(":", ""))
    state.observe(parse(frame("192.168.20.224", wing_sync(True, ours), 319)), 100.0, set())
    assert state.bridging(100.0) == ["192.168.20.224"] and state.ptpv1_heard(100.0)
    assert state.bridging(140.0) == []  # gone quiet for over 30 s


def test_leading_on_its_own_isnt_bridging():
    state = PtpState(own_identity=BOX_A)
    state.observe(parse(frame("192.168.20.246", wing_sync(False, WING_UUID), 319)), 100.0, set())
    assert state.bridging(100.0) == [] and state.devices["192.168.20.246"].v1_leading_at == 100.0


def test_a_ptpv1_follower():
    state = PtpState(own_identity=BOX_A)
    request = ptpv1.header(ptpv1.DELAY_REQ, 5, WING_UUID, 0x0C) + bytes(84)
    state.observe(parse(frame("192.168.20.224", request, 319)), 50.0, set())
    assert state.devices["192.168.20.224"].v1_following_at == 50.0


def test_messages():
    uuid = bytes.fromhex(BOX_A_MAC.replace(":", ""))
    sync = ptpv1.sync_message((1794862122, 12727300), uuid, 74)
    assert len(sync) == 124 and sync[32] == ptpv1.SYNC and sync[35] == 0x18  # ASSIST and EXT_SYNC
    assert sync[67] == 1 and sync[68:71] == b"GPS" and sync[77] == 1 and struct.unpack("b", sync[83:84])[0] == -2
    assert struct.unpack_from("!h", sync, 50)[0] == 0  # a UTC offset of 0, as Dante announces
    follow_up = ptpv1.follow_up_message((1794862122, 12917800), uuid, 74)  # nanoseconds kept exactly
    assert len(follow_up) == 52 and struct.unpack_from("!HII", follow_up, 42) == (74, 1794862122, 12917800)
    response = ptpv1.delay_resp_message((1794862122, 452458600), uuid, WING_UUID, 1, 64451)
    assert len(response) == 60 and response[50:56] == WING_UUID and struct.unpack_from("!H", response, 58)[0] == 64451


def test_delay_requests_and_timestamps():
    request = ptpv1.header(ptpv1.DELAY_REQ, 64451, WING_UUID, 0x0C) + bytes(84)
    assert ptpv1.parse_delay_req(request) == (WING_UUID, 1, 64451)
    assert ptpv1.parse_delay_req(ptpv1.sync_message((1, 0), WING_UUID, 1)) is None
    hardware = struct.pack("qqqqqq", 5, 0, 0, 0, 1794862122, 452458601)
    software = struct.pack("qqqqqq", 1794862122, 452500000, 0, 0, 0, 0)
    assert ptpv1.timestamp(hardware) == ((1794862122, 452458601), "hardware")  # to the nanosecond
    assert ptpv1.timestamp(software)[1] == "software" and ptpv1.timestamp(bytes(48)) == (None, "none")
