import asyncio
import json
import time

from virgil import roles
from virgil.generate import Role
from virgil.request import TimingRequest, to_json
from virgil.timing import Target, TimingManager

LEADER_ID = "b" * 32
OTHER_ID = "c" * 32
ADDRESSES = {LEADER_ID: "10.0.0.2", OTHER_ID: "10.0.0.3"}


class FakeSystemd:
    def __init__(self, active=False):
        self.active = active

    def is_active(self, unit):
        return self.active


def build(target):
    if target.role is Role.LEADER:
        return TimingRequest(Role.LEADER, "enp1s0", None, ("10.0.0.0/24",), 0)
    address = ADDRESSES.get(target.leader_id)
    if address is None:
        return None
    return TimingRequest(Role.FOLLOWER, "enp1s0", address, ("10.0.0.0/24",), 0)


class Recorder:
    def __init__(self, delay=0.0, fail=0):
        self.calls = []
        self.delay = delay
        self.fail = fail

    def __call__(self, old, new, systemd, write, log):
        time.sleep(self.delay)
        if self.fail:
            self.fail -= 1
            raise roles.ServiceError("systemctl start failed")
        self.calls.append((old, new))
        write(new) if new is not None else None


def manager(tmp_path, apply, systemd=None, acked=None):
    acked = acked if acked is not None else []
    m = TimingManager(build, acked.append, systemd=systemd or FakeSystemd(),
                      request_path=tmp_path / "request.json", state_path=tmp_path / "state.json",
                      apply=apply, retry_delay=0.05)
    return m, acked


async def _run(m, *steps, settle=0.3):
    task = asyncio.create_task(m.run())
    try:
        for step in steps:
            step()
            await asyncio.sleep(settle)
    finally:
        task.cancel()


def test_become_leader(tmp_path):
    apply = Recorder()
    m, acked = manager(tmp_path, apply)
    asyncio.run(_run(m, lambda: m.want(Target(Role.LEADER))))
    assert [new.role for _, new in apply.calls] == [Role.LEADER]
    assert apply.calls[0][0] is None
    assert acked == [Target(Role.LEADER)]
    assert json.loads((tmp_path / "state.json").read_text()) == {"role": "leader", "leader_id": None}


def test_asking_again_changes_nothing_but_is_acknowledged(tmp_path):
    apply = Recorder()
    m, acked = manager(tmp_path, apply)
    target = Target(Role.FOLLOWER, LEADER_ID)
    asyncio.run(_run(m, lambda: m.want(target), lambda: m.want(target)))
    assert len(apply.calls) == 1
    assert acked == [target, target]


def test_follower_to_leader_passes_the_running_request(tmp_path):
    apply = Recorder()
    m, _ = manager(tmp_path, apply)
    asyncio.run(_run(m, lambda: m.want(Target(Role.FOLLOWER, LEADER_ID)), lambda: m.want(Target(Role.LEADER))))
    (old1, new1), (old2, new2) = apply.calls
    assert old2 == new1 and new2.role is Role.LEADER


def test_a_change_of_mind_mid_change_goes_to_the_latest(tmp_path):
    apply = Recorder(delay=0.2)
    m, acked = manager(tmp_path, apply)

    async def scenario():
        task = asyncio.create_task(m.run())
        m.want(Target(Role.FOLLOWER, LEADER_ID))
        await asyncio.sleep(0.05)
        m.want(Target(Role.FOLLOWER, OTHER_ID))
        await asyncio.sleep(0.05)
        m.want(Target(Role.LEADER))
        await asyncio.sleep(0.8)
        task.cancel()

    asyncio.run(scenario())
    assert apply.calls[-1][1].role is Role.LEADER
    assert m.applied == Target(Role.LEADER)
    assert acked[-1] == Target(Role.LEADER)
    # Only the final decision is acknowledged; the abandoned ones aren't.
    assert Target(Role.FOLLOWER, OTHER_ID) not in acked


def test_waits_for_the_leaders_address(tmp_path):
    apply = Recorder()
    m, acked = manager(tmp_path, apply)
    unknown = "d" * 32

    async def scenario():
        task = asyncio.create_task(m.run())
        m.want(Target(Role.FOLLOWER, unknown))
        await asyncio.sleep(0.3)
        assert apply.calls == [] and acked == []
        ADDRESSES[unknown] = "10.0.0.4"
        await asyncio.sleep(1.5)
        task.cancel()

    try:
        asyncio.run(scenario())
    finally:
        ADDRESSES.pop(unknown, None)
    assert apply.calls[0][1].leader == "10.0.0.4"
    assert acked == [Target(Role.FOLLOWER, unknown)]


def test_retries_after_a_failure(tmp_path):
    apply = Recorder(fail=2)
    m, acked = manager(tmp_path, apply)
    asyncio.run(_run(m, lambda: m.want(Target(Role.LEADER)), settle=0.5))
    assert len(apply.calls) == 1 and acked == [Target(Role.LEADER)]


def test_stop(tmp_path):
    apply = Recorder()
    m, acked = manager(tmp_path, apply)
    asyncio.run(_run(m, lambda: m.want(Target(Role.LEADER)), lambda: m.want(None)))
    assert apply.calls[-1][1] is None
    assert not (tmp_path / "state.json").exists()
    assert acked[-1] is None


def test_adopts_what_is_already_running(tmp_path):
    request = TimingRequest(Role.FOLLOWER, "enp1s0", "10.0.0.2", ("10.0.0.0/24",), 0)
    (tmp_path / "request.json").write_text(to_json(request))
    (tmp_path / "state.json").write_text(json.dumps({"role": "follower", "leader_id": LEADER_ID}))
    apply = Recorder()
    m, acked = manager(tmp_path, apply, systemd=FakeSystemd(active=True))
    assert m.request == request
    assert m.applied == Target(Role.FOLLOWER, LEADER_ID)
    # The election confirming the same role changes nothing.
    asyncio.run(_run(m, lambda: m.want(Target(Role.FOLLOWER, LEADER_ID))))
    assert apply.calls == []
    assert acked == [Target(Role.FOLLOWER, LEADER_ID)]


def test_nothing_running_means_nothing_adopted(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"role": "leader", "leader_id": None}))
    m, _ = manager(tmp_path, Recorder(), systemd=FakeSystemd(active=False))
    assert m.request is None and m.applied is None
