import pytest

from virgil.election import State
from virgil.heartbeat import Heartbeat, HeartbeatError, decode

BEAT = Heartbeat("a" * 32, "Virgil A", "0.1.0", State.FOLLOWER, 12345, "b" * 32, False, None)


def test_round_trip():
    assert decode(BEAT.encode()) == BEAT


def test_view():
    view = BEAT.view(heard_at=5.0)
    assert view.id == BEAT.id and view.state is State.FOLLOWER and view.heard_at == 5.0


@pytest.mark.parametrize(
    "change",
    [
        {"id": "nope"},
        {"name": ""},
        {"name": "x" * 65},
        {"version": 3},
        {"state": "emperor"},
        {"coin": -1},
        {"coin": 2**64},
        {"coin": True},
        {"leader": "someone"},
        {"handover_to": 5},
        {"lost_leader": "no"},
        {"observing": 1},
    ],
)
def test_bad_heartbeats(change):
    with pytest.raises(HeartbeatError):
        decode({**BEAT.encode(), **change})


def test_not_an_object():
    with pytest.raises(HeartbeatError):
        decode([1, 2])


def test_established_round_trip():
    from dataclasses import replace

    beat = replace(BEAT, state=State.LEADER, leader=None, established=True)
    decoded = decode(beat.encode())
    assert decoded.established and decoded.view(1.0).established


def test_older_heartbeats_without_established():
    data = BEAT.encode()
    del data["established"]
    assert decode(data).established is False


def test_ptp_fields_round_trip():
    from dataclasses import replace

    beat = replace(BEAT, ptp_identity="aa:bb:cc:ff:fe:00:00:01", ptp_warnings=("PTP from Box B is crossing the link",))
    assert decode(beat.encode()) == beat


@pytest.mark.parametrize(
    "change",
    [{"ptp_identity": "nope"}, {"ptp_warnings": "x"}, {"ptp_warnings": ["x"] * 9}, {"ptp_warnings": ["x" * 201]}],
)
def test_bad_ptp_fields(change):
    with pytest.raises(HeartbeatError):
        decode({**BEAT.encode(), **change})


def test_clock_round_trip():
    from dataclasses import replace

    beat = replace(BEAT, clock={"source": "10.10.100.253", "system_offset_ns": 307, "phc_locked": True})
    assert decode(beat.encode()).clock == beat.clock


@pytest.mark.parametrize("clock", ["fast", {"a": [1]}, {str(i): i for i in range(17)}, {"x": "y" * 3000}])
def test_bad_clock(clock):
    with pytest.raises(HeartbeatError):
        decode({**BEAT.encode(), "clock": clock})


def test_alerts_round_trip():
    from dataclasses import replace

    beat = replace(BEAT, alerts=({"label": "LOCK LOST", "device": "WingCompact", "title": "x"},))
    assert decode(beat.encode()).alerts == beat.alerts
    with pytest.raises(HeartbeatError):
        decode({**BEAT.encode(), "alerts": ["x"] * 3})


def test_recording_round_trip():
    from dataclasses import replace

    beat = replace(BEAT, recording={"active": True, "size": 12_400_000})
    assert decode(beat.encode()).recording == beat.recording
    with pytest.raises(HeartbeatError):
        decode({**BEAT.encode(), "recording": {"active": "yes please"}})
