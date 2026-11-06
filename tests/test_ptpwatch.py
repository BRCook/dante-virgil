import socket
import struct

import pytest

from virgil.ptpwatch import ANNOUNCE, DELAY_REQ, SYNC, PtpState, _filter, parse

OWN_GM = "aa:bb:cc:ff:fe:00:00:01"
MEMBER_GM = "aa:bb:cc:ff:fe:00:00:02"
FOREIGN_GM = "00:1d:c1:ff:fe:12:34:56"


def frame(source="10.10.100.50", port=319, payload=b"", fragment=0, protocol=17):
    udp = struct.pack("!HHHH", 50000, port, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, fragment, 64, protocol, 0,
                     socket.inet_aton(source), socket.inet_aton("224.0.1.129"))
    return b"\x01\x00\x5e\x00\x01\x81" + b"\x00\x11\x22\x33\x44\x55" + b"\x08\x00" + ip + udp


def v2(message_type, clock_identity="00:1d:c1:ff:fe:aa:bb:cc", grandmaster=None):
    header = bytearray(34)
    header[0] = message_type
    header[1] = 0x02
    header[20:28] = bytes.fromhex(clock_identity.replace(":", ""))
    body = bytearray(30)
    if grandmaster:
        body[19:27] = bytes.fromhex(grandmaster.replace(":", ""))  # offset 53 overall
    return bytes(header + body)


V1 = b"\x00\x01" + bytes(60)


# A tiny classic-BPF interpreter, enough for Virgil's filter.
def run_filter(packet: bytes) -> int:
    program, pc, a, x = _filter(), 0, 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x28:
            a = struct.unpack_from("!H", packet, k)[0]
        elif code == 0x30:
            a = packet[k]
        elif code == 0xB1:
            x = (packet[k] & 0x0F) * 4
        elif code == 0x48:
            a = struct.unpack_from("!H", packet, x + k)[0]
        elif code == 0x15:
            pc += jt if a == k else jf
        elif code == 0x45:
            pc += jt if a & k else jf
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")
        pc += 1


@pytest.mark.parametrize("port", [319, 320])
def test_filter_passes_ptp(port):
    assert run_filter(frame(port=port, payload=V1)) > 0


@pytest.mark.parametrize(
    "packet",
    [
        frame(port=5004, payload=b"audio" * 20),  # Dante or AES67 audio
        frame(port=4440, payload=b"control"),
        frame(protocol=6, payload=V1),  # TCP
        frame(fragment=0x0010, payload=V1),  # a later fragment
        b"\xff" * 12 + b"\x86\xdd" + bytes(60),  # IPv6
    ],
)
def test_filter_drops_everything_else(packet):
    assert run_filter(packet) == 0


def test_parse_v1():
    packet = parse(frame(payload=V1))
    assert packet.version == 1 and packet.source == "10.10.100.50" and packet.message_type is None


def test_parse_v2_delay_request():
    packet = parse(frame(payload=v2(DELAY_REQ)))
    assert packet.version == 2 and packet.message_type == DELAY_REQ
    assert packet.clock_identity == "00:1d:c1:ff:fe:aa:bb:cc" and packet.grandmaster is None


def test_parse_v2_announce():
    packet = parse(frame(port=320, payload=v2(ANNOUNCE, grandmaster=FOREIGN_GM)))
    assert packet.message_type == ANNOUNCE and packet.grandmaster == FOREIGN_GM


def test_parse_junk():
    assert parse(b"short") is None
    assert parse(frame(payload=b"\x05")) is None
    assert parse(frame(payload=b"\x00\x07" + bytes(40))) is None


def test_learning():
    state = PtpState()
    own = {"10.10.100.253"}
    # Our own Announce, going out.
    state.observe(parse(frame("10.10.100.253", 320, v2(ANNOUNCE, grandmaster=OWN_GM)), outgoing=True), 1.0, own)
    assert state.own_identity == OWN_GM and state.devices == {}
    # The console speaks PTPv1 first, then starts following this Virgil.
    state.observe(parse(frame(payload=V1)), 2.0, own)
    assert not state.devices["10.10.100.50"].follows_this_virgil
    state.observe(parse(frame(payload=v2(DELAY_REQ))), 3.0, own)
    console = state.devices["10.10.100.50"]
    assert console.ptpv1 and console.follows_this_virgil and console.last_seen == 3.0
    # Sync messages from the console don't make it a follower, and don't add devices.
    state.observe(parse(frame("10.10.100.60", payload=v2(SYNC))), 3.5, own)
    assert not state.devices["10.10.100.60"].follows_this_virgil
    assert state.foreign == {}


def test_leaks_and_foreign_grandmasters():
    state = PtpState(own_identity=OWN_GM)
    state.observe(parse(frame("10.10.100.211", 320, v2(ANNOUNCE, grandmaster=MEMBER_GM))), 1.0, set())
    state.observe(parse(frame("10.10.100.77", 320, v2(ANNOUNCE, grandmaster=FOREIGN_GM))), 1.0, set())
    warnings = state.leaks({MEMBER_GM: "Box B"})
    assert any("PTP from Box B is crossing the link" in w for w in warnings)
    assert any(FOREIGN_GM in w and "Another PTP grandmaster" in w for w in warnings)
    assert "10.10.100.211" not in state.devices


def test_forgetting():
    state = PtpState()
    state.observe(parse(frame(payload=V1)), 0.0, set())
    state.observe(parse(frame("10.10.100.77", 320, v2(ANNOUNCE, grandmaster=FOREIGN_GM))), 0.0, set())
    state.forget_old(10.0)
    assert state.devices and state.foreign
    state.forget_old(31.0)
    assert not state.devices and not state.foreign


def test_live_filter_on_loopback():
    """With CAP_NET_RAW (such as running as root), check the real kernel filter."""
    import time

    from virgil.ptpwatch import ETH_P_IP, SNAPLEN, attach_filter

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    except PermissionError:
        pytest.skip("needs CAP_NET_RAW")
    with sock:
        attach_filter(sock)
        sock.bind(("lo", ETH_P_IP))
        sock.setblocking(False)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"audio audio", ("127.0.0.1", 5004))
            sender.sendto(V1, ("127.0.0.1", 320))
        time.sleep(0.2)
        seen = []
        while True:
            try:
                data, _ = sock.recvfrom(SNAPLEN)
            except BlockingIOError:
                break
            seen.append(parse(data))
    assert [p.version for p in seen if p is not None] == [1]
