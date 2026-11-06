import subprocess
from dataclasses import replace

import pytest

from virgil.generate import Role, Tuning
from virgil.request import TimingRequest
from virgil.roles import (
    CHRONY,
    PHC2SYS,
    PREPARE,
    PTP4L,
    SYNC,
    ServiceError,
    Step,
    Systemd,
    apply,
    transition,
)

LEADER = TimingRequest(Role.LEADER, "enp1s0", None, ("192.168.20.0/24",), 21500)
FOLLOWER = TimingRequest(Role.FOLLOWER, "enp1s0", "192.168.20.21", ("192.168.20.21",), 21500)

W = Step("write")


def start(unit, block=True):
    return Step("start", unit, block)


def stop(unit):
    return Step("stop", unit)


def restart(unit, block=True):
    return Step("restart", unit, block)


def test_start_as_leader():
    assert transition(None, LEADER) == [
        W, start(PREPARE), stop(PHC2SYS), stop(PTP4L), stop(CHRONY), start(SYNC), start(CHRONY), start(PTP4L),
    ]


def test_start_as_follower():
    assert transition(None, FOLLOWER) == [
        W, start(PREPARE), stop(PHC2SYS), stop(PTP4L), stop(CHRONY), start(SYNC), start(CHRONY),
        start(PHC2SYS, block=False), start(PTP4L),
    ]


def test_follower_to_leader_leaves_ptp4l_alone():
    assert transition(FOLLOWER, LEADER) == [W, start(PREPARE), stop(PHC2SYS), restart(CHRONY), start(PTP4L)]


def test_leader_to_follower_leaves_ptp4l_alone():
    assert transition(LEADER, FOLLOWER) == [
        W, start(PREPARE), restart(CHRONY), start(PHC2SYS, block=False), start(PTP4L),
    ]


def test_new_leader_for_a_follower():
    new = replace(FOLLOWER, leader="192.168.20.23", allow=("192.168.20.23",))
    assert transition(FOLLOWER, new) == [W, start(PREPARE), restart(CHRONY), start(PTP4L)]


@pytest.mark.parametrize(
    "change",
    [{"ptp_interface": "enp2s0"}, {"trim_ppb": 22000}, {"use_nic_profile": False}],
)
def test_phc_changes_start_from_scratch(change):
    steps = transition(FOLLOWER, replace(FOLLOWER, **change))
    assert start(SYNC) in steps and stop(PTP4L) in steps


def test_phc2sys_tuning_restarts_phc2sys_only():
    new = replace(FOLLOWER, tuning=Tuning(phc2sys_kp=0.05))
    assert transition(FOLLOWER, new) == [
        W, start(PREPARE), restart(CHRONY), restart(PHC2SYS, block=False), start(PTP4L),
    ]


def test_ptp4l_tuning_restarts_ptp4l():
    new = replace(LEADER, tuning=Tuning(log_sync_interval=-4))
    assert transition(LEADER, new)[-1] == restart(PTP4L)


def test_stop():
    assert transition(FOLLOWER, None) == [stop(PHC2SYS), stop(PTP4L), stop(CHRONY)]


class FakeRun:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = fail

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        failed = any(word in args for word in self.fail)
        return subprocess.CompletedProcess(args, 1 if failed else 0, "", "Unit not found." if failed else "")


def test_apply_runs_systemctl_in_order():
    run = FakeRun()
    written = []
    apply(FOLLOWER, LEADER, Systemd(run), write=written.append)
    assert written == [LEADER]
    assert run.calls == [
        ["systemctl", "start", PREPARE],
        ["systemctl", "stop", PHC2SYS],
        ["systemctl", "restart", CHRONY],
        ["systemctl", "start", PTP4L],
    ]


def test_non_blocking_start():
    run = FakeRun()
    Systemd(run).start(PHC2SYS, block=False)
    assert run.calls == [["systemctl", "start", "--no-block", PHC2SYS]]


def test_a_failure_stops_apply():
    run = FakeRun(fail=(PREPARE,))
    with pytest.raises(ServiceError, match="Unit not found"):
        apply(None, LEADER, Systemd(run), write=lambda r: None)
    assert len(run.calls) == 1
