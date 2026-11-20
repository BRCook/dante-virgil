import pytest
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


def test_each_chart_gets_the_summary_that_suits_it():
    from virgil.graphs import summary

    readings = [(t, v) for t, v in enumerate([1.0, 2.0, 3.0, 4.0, 100.0])]
    assert summary("offset", "offset/A", [(0, 5.0), (1, -9.0), (2, 3.0)]) == {"kind": "peak", "value": -9.0}
    assert summary("latency", "latency/WingCompact", readings) == {"kind": "max", "value": 100.0}
    median = summary("latency", "average/WingCompact", readings)
    assert median["kind"] == "median" and median["value"] == 3.0  # the spike doesn't move the median...
    assert median["spread"] == pytest.approx(61.6 - 1.4)  # ...and the spread is the middle 80% (10th to 90th)
    assert summary("latency", "setting/WingCompact", readings) is None
    assert summary("console", "console/WingCompact", readings) == {"kind": "range", "low": 1.0, "high": 100.0}
    assert summary("loss", "loss/A-B", [(0, 0.0), (1, 100.0)]) == {"kind": "mean", "value": 50.0}


def test_late_packets_are_totalled_from_their_rates():
    from virgil.graphs import summary

    # Each reading is a rate since the one before: 2/s for 5 s, then 0, then 10/s for 5 s: 60 in all.
    readings = [(0.0, 0.0), (5.0, 2.0), (10.0, 0.0), (15.0, 10.0)]
    assert summary("late", "late/WingRack", readings) == {"kind": "total", "value": 60.0}


def test_summaries_come_from_every_reading_not_the_thinned_points():
    """A one-off spike that thinning averages away is still in the summary."""
    from virgil.graphs import Recorder

    recorder = Recorder()
    for second in range(600):
        recorder.add("offset/A", "offset", "A", "ns", 1000.0 + second, 50_000.0 if second == 300 else 10.0)
    series = recorder.window(600, 1600.0, max_points=10)[0]
    assert max(abs(v) for _, v in series["points"]) < 50_000  # thinned by averaging...
    assert series["summary"] == {"kind": "peak", "value": 50_000.0}  # ...but not the summary


def test_a_series_measured_four_times_a_second_keeps_ten_minutes():
    """The link is pinged four times a second; its charts once stopped at five minutes."""
    from virgil.graphs import KEEP_SECONDS, Recorder

    recorder = Recorder()
    for quarter in range(KEEP_SECONDS * 4):
        recorder.add("rtt/A-B", "rtt", "Box A to Box B", "ns", 1000.0 + quarter / 4, 1_000_000.0)
    points = recorder.series["rtt/A-B"].points
    assert points[-1][0] - points[0][0] >= KEEP_SECONDS - 1


def test_a_reply_is_timed_by_the_kernel_not_when_its_read():
    import socket
    import struct

    from virgil import pinger

    arrived = 5_000_000_000 + 1_200_000  # the kernel says: 1.2 ms after sending
    ancillary = [(socket.SOL_SOCKET, pinger.SO_TIMESTAMPNS, struct.pack("qq", 5, 1_200_000))]
    assert pinger.received_at(ancillary) == arrived
    assert pinger.received_at([]) is None

    class Socket:
        def __init__(self):
            self.replies = [(bytes([0, 0, 0, 0, 0, 0, 0, 7]) + b"virgil", ancillary, 0, None)]

        def recvmsg(self, size, ancillary_size):
            if not self.replies:
                raise BlockingIOError
            return self.replies.pop()

    results = []
    p = pinger.Pinger(lambda address, rtt: results.append(rtt), clock=lambda: 5_000_000_000 + 25_000_000)
    p._sock = Socket()
    p._sent[7] = ("10.0.0.2", 5_000_000_000)
    p._readable()  # read 25 ms after sending, as a busy virgild might
    assert results == [1_200_000.0]  # but timed by its arrival: 1.2 ms
