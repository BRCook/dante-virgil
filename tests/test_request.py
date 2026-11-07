import json

import pytest

from virgil.generate import Role, Tuning
from virgil.request import RequestError, TimingRequest, from_json, to_json

FOLLOWER = {
    "role": "follower",
    "ptp_interface": "enp1s0",
    "leader": "192.168.20.21",
    "allow": ["192.168.20.21"],
    "trim_ppb": 22800,
}


def test_round_trip():
    request = TimingRequest(
        role=Role.FOLLOWER,
        ptp_interface="enp1s0",
        leader="192.168.20.21",
        allow=("192.168.20.0/24", "10.0.0.5"),
        trim_ppb=-500,
        use_nic_profile=False,
        tuning=Tuning(maxdelayquant=0.5),
    )
    assert from_json(to_json(request)) == request


def test_defaults():
    request = from_json(json.dumps(FOLLOWER))
    assert request.use_nic_profile is True
    assert request.tuning == Tuning()


def test_addresses_are_normalised():
    request = from_json(json.dumps({**FOLLOWER, "allow": ["192.168.20.7/24", "10.0.0.1/32"]}))
    assert request.allow == ("192.168.20.0/24", "10.0.0.1")


def _with(**changes):
    data = {**FOLLOWER, **changes}
    return json.dumps({k: v for k, v in data.items() if v is not ...})


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        _with(role="boss"),
        _with(role=...),
        _with(ptp_interface="enp1s0; rm -rf /"),
        _with(ptp_interface="../etc"),
        _with(ptp_interface="a-very-long-interface-name"),
        _with(leader="virgil-a.local"),
        _with(leader="192.168.20.21\nlocal stratum 1"),
        _with(leader=None),
        _with(role="leader"),
        _with(allow="192.168.20.0/24"),
        _with(allow=["everyone"]),
        _with(allow=["10.0.0.1"] * 65),
        _with(trim_ppb=1.5),
        _with(trim_ppb=True),
        _with(trim_ppb=5_000_000),
        _with(use_nic_profile="yes"),
        _with(tuning={"maxdelayquant": 5}),
        _with(tuning={"minsamples": 300, "maxsamples": 256}),
        _with(tuning={"minsamples": 64.5}),
        _with(tuning={"bogus": 1}),
        _with(tuning={"phc2sys_kp": True}),
        _with(extra="field"),
    ],
)
def test_bad_requests_are_rejected(text):
    with pytest.raises(RequestError):
        from_json(text)


def test_a_leader_needs_no_leader():
    request = from_json(_with(role="leader", leader=None))
    assert request.role is Role.LEADER and request.leader is None


def test_ntp_hwtimestamp():
    request = from_json(_with(ntp_hwtimestamp="enp1s0"))
    assert request.ntp_hwtimestamp == "enp1s0"
    assert from_json(to_json(request)) == request
    with pytest.raises(RequestError):
        from_json(_with(ntp_hwtimestamp="enp1s0; reboot"))
