import struct

from virgil.graphs import MEAN, WORST, Recorder, jitter
from virgil.pinger import _checksum, echo_request, parse_reply


def test_window_and_downsampling():
    recorder = Recorder()
    for second in range(600):
        recorder.add("offset/Box B", "offset", "Box B", "ns", 1000.0 + second, (-1) ** second * second)
    (series,) = recorder.window(60, now=1600.0, max_points=30)
    assert series["chart"] == "offset" and series["unit"] == "ns"
    assert len(series["points"]) == 30
    assert -60 <= series["points"][0][0] < -58 and series["points"][-1][0] < 0  # times relative to now


def test_the_worst_value_survives_downsampling():
    recorder = Recorder()
    for second in range(100):
        recorder.add("latency/x", "latency", "x", "ns", float(second), 9_000_000 if second == 37 else 1_000_000, WORST)
        recorder.add("console/x", "console", "x", "ppm", float(second), 9.0 if second == 37 else 1.0, MEAN)
    series = {s["key"]: s for s in recorder.window(100, now=100.0, max_points=10)}
    assert max(v for _, v in series["latency/x"]["points"]) == 9_000_000  # a spike isn't averaged away
    assert max(v for _, v in series["console/x"]["points"]) < 9.0


def test_forgetting_and_nothing_to_show():
    recorder = Recorder()
    recorder.add("a", "c", "a", "ns", 10.0, 1.0)
    recorder.add("b", "c", "b", "ns", 10.0, None)  # no value: nothing recorded
    recorder.forget_before(20.0)
    assert recorder.series == {} and recorder.window(60, 100.0) == []


def test_jitter():
    j = None
    last = None
    for rtt in (1000, 1000, 1000):
        j = jitter(j, rtt, last)
        last = rtt
    assert j == 0.0
    j = jitter(j, 2600, 1000)
    assert j == 100.0  # 1/16 of the 1,600 ns change


def test_echo_request_and_reply():
    request = echo_request(0x1234, 7)
    assert request[0] == 8 and _checksum(request) == 0  # a valid ICMP checksum sums to zero
    reply = bytes([0, 0, 0, 0]) + struct.pack("!HH", 0x1234, 7) + b"virgil"
    assert parse_reply(reply) == 7
    with_ip_header = bytes([0x45]) + bytes(19) + reply
    assert parse_reply(with_ip_header) == 7
    assert parse_reply(bytes([8]) + bytes(7)) is None  # a request, not a reply
