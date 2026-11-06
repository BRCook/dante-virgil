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
