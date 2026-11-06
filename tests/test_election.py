"""Simulated groups of boxes, exchanging heartbeats over links that can be cut."""

import itertools
import random

import pytest

from virgil.election import Election, PeerView, State, Timing

STEP = 0.5
HEARTBEAT = 1.0


class Group:
    """Coins listed for a box are drawn in order: at boot, then each time it becomes leader."""

    def __init__(self, coins: dict[str, list[int]] | None = None, ids=("A", "B"), seed=1):
        self.t = 0.0
        self.rng = random.Random(seed)
        self.coins = coins or {}
        self.boxes: dict[str, Election] = {}
        self.inbox: dict[str, dict[str, PeerView]] = {}
        self.links: set[frozenset] = set()
        self.down: set[str] = set()
        self.history: list[tuple[float, str, State, str | None]] = []
        for box_id in ids:
            self.boot(box_id)

    def _draw(self, box_id):
        planned = iter(self.coins.get(box_id, []))
        return lambda: next(planned, None) or self.rng.getrandbits(64)

    def boot(self, box_id):
        self.boxes[box_id] = Election(box_id, self.t, draw=self._draw(box_id))
        self.inbox[box_id] = {}
        self.down.discard(box_id)

    def power_off(self, box_id):
        self.down.add(box_id)

    def connect(self, *ids):
        for a, b in itertools.combinations(ids or self.boxes, 2):
            self.links.add(frozenset((a, b)))

    def cut(self, a, b):
        self.links.discard(frozenset((a, b)))

    def cut_all(self):
        self.links.clear()

    def run(self, seconds, check=None):
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.t = round(self.t + STEP, 6)
            if abs(self.t / HEARTBEAT - round(self.t / HEARTBEAT)) < 1e-9:
                for a, b in itertools.permutations(self.boxes, 2):
                    if a not in self.down and b not in self.down and frozenset((a, b)) in self.links:
                        self.inbox[b][a] = self.boxes[a].view(heard_at=self.t)
            for box_id, box in self.boxes.items():
                if box_id in self.down:
                    continue
                # Syncing takes a second.
                if box.state is State.SYNC and self.t - box.since >= 1.0:
                    self._record(box_id, box.synced(self.t))
                self._record(box_id, box.tick(self.inbox[box_id], self.t))
            if check:
                check(self)

    def _record(self, box_id, change):
        if change is not None:
            self.history.append((self.t, box_id, change.new_state, change.new_leader))

    def state(self, box_id):
        return self.boxes[box_id].state, self.boxes[box_id].leader

    def leaders(self):
        return sorted(i for i, b in self.boxes.items() if i not in self.down and b.state is State.LEADER)

    def became_leader(self, box_id):
        return [t for t, i, s, _ in self.history if i == box_id and s is State.LEADER]


def one_settled_leader(group):
    """Within a connected group, never two leaders that aren't handing over."""
    settled = [i for i in group.leaders() if group.boxes[i].handover_to is None]
    assert len(settled) <= 1, f"two leaders at {group.t}: {settled}"


def test_cold_start_together_highest_coin_leads():
    group = Group({"A": [10], "B": [20]})
    group.connect()
    group.run(15, check=one_settled_leader)
    assert group.leaders() == ["B"]
    assert group.state("A") == (State.FOLLOWER, "B")
    assert group.became_leader("A") == []


def test_radio_comes_up_last():
    group = Group({"A": [10, 30], "B": [20, 40]})
    group.run(15)
    assert group.leaders() == ["A", "B"]
    group.connect()
    group.run(5)
    # Coins are redrawn on becoming leader, so B (40) beats A (30).
    assert group.leaders() == ["B"]
    assert group.state("A") == (State.FOLLOWER, "B")


def test_the_incumbent_stays_leader():
    group = Group({"A": [10], "B": [999]}, ids=("A",))
    group.run(15)
    assert group.leaders() == ["A"]
    group.boot("B")
    group.connect("A", "B")
    group.run(10, check=one_settled_leader)
    assert group.leaders() == ["A"]
    assert group.state("B") == (State.FOLLOWER, "A")
    assert group.became_leader("B") == []


def test_a_follower_reboots():
    group = Group({"A": [20], "B": [10]})
    group.connect()
    group.run(15)
    group.power_off("B")
    group.run(20)
    group.boot("B")
    group.run(10, check=one_settled_leader)
    assert group.leaders() == ["A"]
    assert group.state("B") == (State.FOLLOWER, "A")


def test_a_brief_dropout_changes_nothing():
    group = Group({"A": [20], "B": [10]})
    group.connect()
    group.run(15)
    before = len(group.history)
    group.cut("A", "B")
    group.run(20)
    group.connect()
    group.run(10)
    assert len(group.history) == before
    assert group.state("B") == (State.FOLLOWER, "A")


def test_the_leader_is_lost_after_the_timeout():
    group = Group({"A": [20], "B": [10]})
    group.connect()
    group.run(15)
    group.power_off("A")
    group.run(Timing().leader_lost - 2)
    assert group.state("B") == (State.FOLLOWER, "A")
    group.run(10)
    assert group.leaders() == ["B"]


def test_followers_that_lose_the_leader_agree_on_one():
    group = Group({"A": [99], "B": [10, 10], "C": [20, 20]}, ids=("A", "B", "C"))
    group.connect()
    group.run(15)
    assert group.leaders() == ["A"]
    group.power_off("A")
    group.run(45, check=one_settled_leader)
    assert group.leaders() == ["C"]
    assert group.state("B") == (State.FOLLOWER, "C")
    assert group.became_leader("B") == []


def test_the_old_leader_returns_as_a_follower():
    group = Group({"A": [20], "B": [10]})
    group.connect()
    group.run(15)
    group.power_off("A")
    group.run(40)
    group.boot("A")
    group.run(10, check=one_settled_leader)
    assert group.leaders() == ["B"]
    assert group.state("A") == (State.FOLLOWER, "B")


def test_islands_merge_and_the_losers_followers_switch():
    group = Group({"A": [50, 50], "B": [1], "C": [40, 40], "D": [2]}, ids=("A", "B", "C", "D"))
    group.connect("A", "B")
    group.connect("C", "D")
    group.run(15)
    assert group.leaders() == ["A", "C"]
    group.connect()
    group.run(5)
    assert group.leaders() == ["A"]
    for box_id in "BCD":
        assert group.state(box_id) == (State.FOLLOWER, "A")


def test_a_split_group_leads_on_both_sides_then_rejoins():
    group = Group({"A": [50, 50], "B": [10, 60], "C": [5]}, ids=("A", "B", "C"))
    group.connect()
    group.run(15)
    assert group.leaders() == ["A"]
    # B and C are on the far side of the radio from A.
    group.cut("A", "B")
    group.cut("A", "C")
    group.run(40)
    assert group.leaders() == ["A", "B"]
    assert group.state("C") == (State.FOLLOWER, "B")
    group.connect()
    group.run(5)
    # B's coin was redrawn (60) when it took over, so it beats A (50).
    assert group.leaders() == ["B"]
    assert group.state("A") == (State.FOLLOWER, "B")


def test_hand_over():
    group = Group({"A": [50, 50], "B": [10, 5], "C": [20]}, ids=("A", "B", "C"))
    group.connect()
    group.run(15)
    group.boxes["A"].hand_over("B")
    group.run(5, check=one_settled_leader)
    assert group.leaders() == ["B"]
    assert group.state("A") == (State.FOLLOWER, "B")
    assert group.state("C") == (State.FOLLOWER, "B")
    # B's new coin (5) is lower than A's, and it still stays leader.
    group.run(10, check=one_settled_leader)
    assert group.leaders() == ["B"]


def test_hand_over_to_a_missing_box_is_cancelled():
    group = Group({"A": [50], "B": [10]})
    group.connect()
    group.run(15)
    group.boxes["A"].hand_over("Z")
    group.run(3)
    assert group.leaders() == ["A"]
    assert group.boxes["A"].handover_to is None


def test_equal_coins_are_redrawn():
    group = Group({"A": [7, 7], "B": [7, 7]})
    group.run(15)
    group.connect()
    group.run(10, check=None)
    assert len(group.leaders()) == 1


def test_stop_and_start():
    group = Group({"A": [20], "B": [10]})
    group.connect()
    group.run(15)
    group.boxes["B"].stop(group.t)
    group.run(5)
    assert group.state("B") == (State.STOPPED, None)
    group.boxes["B"].start(group.t)
    group.run(10)
    assert group.state("B") == (State.FOLLOWER, "A")


@pytest.mark.parametrize("seed", range(20))
def test_random_coins_always_end_with_one_leader(seed):
    group = Group(ids=("A", "B", "C"), seed=seed)
    group.run(12)
    group.connect()
    group.run(10)
    assert len(group.leaders()) == 1
    leader = group.leaders()[0]
    for box_id in "ABC":
        if box_id != leader:
            assert group.state(box_id) == (State.FOLLOWER, leader)


@pytest.mark.parametrize("seed", range(10))
def test_random_outages_always_settle(seed):
    """Cut and restore random links for a while, then reconnect everything."""
    rng = random.Random(seed)
    ids = ("A", "B", "C", "D")
    group = Group(ids=ids, seed=seed)
    group.connect()
    for _ in range(12):
        a, b = rng.sample(ids, 2)
        if rng.random() < 0.5:
            group.cut(a, b)
        else:
            group.connect(a, b)
        if rng.random() < 0.15:
            victim = rng.choice(ids)
            if victim in group.down:
                group.boot(victim)
            else:
                group.power_off(victim)
        group.run(rng.choice([2, 10, 40]))
    for box_id in ids:
        if box_id in group.down:
            group.boot(box_id)
    group.connect()
    group.run(60)
    assert len(group.leaders()) == 1
    leader = group.leaders()[0]
    for box_id in ids:
        if box_id != leader:
            assert group.state(box_id) == (State.FOLLOWER, leader)
