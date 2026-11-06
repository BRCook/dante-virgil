"""Leader election, as a pure state machine.

Each Virgil runs one Election. The daemon feeds it the latest heartbeat from
every other member, and the current time, and carries out whatever change of
state it decides on. Nothing here touches the network, the clocks or systemd,
so every rule can be tested by simulating several Virgils at once.

The rules, from the design:

1. The incumbent stays leader. A Virgil that finds a running leader follows it.
2. If the leader is lost, the followers that lost it (and can still see each
   other) pick the one with the highest coin. It leads; the rest follow it.
3. If leaders see each other, a leader that had heard from the group before it
   started leading beats one that never had (such as a Virgil that booted and
   led alone because it couldn't hear anyone yet). Between equals, the one with
   the highest coin stays leader. The others step down, along with their followers.
4. If no Virgil is leading at startup, the Virgils listening together pick the one
   with the highest coin.
5. A Virgil can be made leader by hand, by asking the current leader to hand over.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum


class State(Enum):
    LISTEN = "listen"
    SYNC = "sync"
    FOLLOWER = "follower"
    LEADER = "leader"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Timing:
    # A member counts as visible if it's been heard from this recently.
    visible: float = 3.0
    # How long a Virgil listens before it decides anything by coin, so it hears
    # every member that's already up.
    listen_settle: float = 3.0
    # How long a Virgil listens for a leader before leading alone.
    listen_timeout: float = 10.0
    # How long a follower goes without hearing its leader before it's lost.
    leader_lost: float = 30.0


@dataclass(frozen=True)
class PeerView:
    """What this Virgil knows about another member, from its latest heartbeat."""

    id: str
    state: State
    coin: int
    # The member it follows, if it's a follower or syncing.
    leader: str | None
    lost_leader: bool
    # A leader handing over names the member it's handing over to.
    handover_to: str | None
    # When the heartbeat arrived, by this Virgil's monotonic clock.
    heard_at: float
    # A leader that had heard from the group before it started leading.
    established: bool = False


@dataclass(frozen=True)
class Change:
    old_state: State
    new_state: State
    old_leader: str | None
    new_leader: str | None


@dataclass
class Election:
    id: str
    now: float
    timing: Timing = Timing()
    draw: Callable[[], int] = field(default=lambda: secrets.randbits(64), repr=False)

    def __post_init__(self) -> None:
        self.coin = self.draw()
        self.state = State.LISTEN
        self.leader: str | None = None
        self.lost_leader = False
        self.lost_since = self.now
        self.handover_to: str | None = None
        self.since = self.now
        # Whether this Virgil has heard from any member since it started.
        self.heard_group = False
        # Whether it had, when it started leading.
        self.established = False

    # What goes into this Virgil's heartbeat.

    def view(self, heard_at: float) -> PeerView:
        return PeerView(self.id, self.state, self.coin, self.leader, self.lost_leader, self.handover_to, heard_at,
                        self.established)

    # Events from the daemon.

    def synced(self, now: float) -> Change | None:
        """The daemon has finished taking the time from the leader."""
        if self.state is not State.SYNC:
            return None
        return self._go(State.FOLLOWER, self.leader, now)

    def stop(self, now: float) -> Change | None:
        if self.state is State.STOPPED:
            return None
        return self._go(State.STOPPED, None, now)

    def start(self, now: float) -> Change | None:
        if self.state is not State.STOPPED:
            return None
        self.coin = self.draw()
        return self._go(State.LISTEN, None, now)

    def resume(self, state: State, leader: str | None, now: float) -> None:
        """Pick up a role the timing services are already running in, after virgild restarts.

        A leader carries on leading (and the coin settles it if it meets
        another). A follower carries on following, and loses its leader in the
        usual way if it's gone.
        """
        if state is State.LEADER:
            # It was leading before virgild restarted, so it's no newcomer.
            self.state, self.leader = State.LEADER, None
            self.established = True
        elif state is State.FOLLOWER and leader is not None:
            self.state, self.leader = State.FOLLOWER, leader
        else:
            return
        self.since = now
        self.heard_group = True
        self.lost_leader = False
        self.handover_to = None

    def listen_from(self, now: float) -> None:
        """Start the listening countdown again, such as once the network is ready."""
        if self.state is State.LISTEN:
            self.since = now

    def hand_over(self, to: str) -> None:
        """Ask this Virgil, if it's leading, to hand over to another member."""
        if self.state is State.LEADER and to != self.id:
            self.handover_to = to

    # The rules.

    def tick(self, peers: Mapping[str, PeerView], now: float) -> Change | None:
        """Apply the rules to the latest heartbeats, and return any change of state."""
        visible = {p.id: p for p in peers.values() if p.id != self.id and now - p.heard_at <= self.timing.visible}
        if visible:
            self.heard_group = True
        if self.state is State.STOPPED:
            return None
        if self.state is State.LISTEN:
            return self._listen(visible, now)
        if self.state in (State.SYNC, State.FOLLOWER):
            return self._follow(peers, visible, now)
        return self._lead(visible, now)

    def _leaders(self, visible: Mapping[str, PeerView]) -> list[PeerView]:
        """Visible leaders worth following, best first. A leader handing over isn't one."""
        leaders = [p for p in visible.values() if p.state is State.LEADER]
        settled = [p for p in leaders if p.handover_to is None]
        return sorted(settled or leaders, key=lambda p: (p.established, p.coin, p.id), reverse=True)

    def _wins(self, rivals: list[PeerView]) -> bool | None:
        """Whether this Virgil's coin beats every rival's. None means a tie, and a redraw."""
        if any(r.coin == self.coin for r in rivals):
            self.coin = self.draw()
            return None
        return all(self.coin > r.coin for r in rivals)

    def _listen(self, visible: Mapping[str, PeerView], now: float) -> Change | None:
        leaders = self._leaders(visible)
        if leaders:
            return self._go(State.SYNC, leaders[0].id, now)
        if now - self.since < self.timing.listen_settle:
            return None
        listening = [p for p in visible.values() if p.state is State.LISTEN]
        if listening:
            if self._wins(listening):
                return self._lead_now(now)
            return None
        if now - self.since >= self.timing.listen_timeout:
            return self._lead_now(now)
        return None

    def _follow(self, peers: Mapping[str, PeerView], visible: Mapping[str, PeerView], now: float) -> Change | None:
        leaders = self._leaders(visible)
        current = visible.get(self.leader)

        # A handover to this Virgil.
        if current is not None and current.state is State.LEADER and current.handover_to == self.id:
            return self._lead_now(now)

        # Follow the best visible leader, which changes if leaders meet (rule 3)
        # or the leader handed over to someone else.
        if leaders:
            self.lost_leader = False
            best = leaders[0]
            if best.id != self.leader:
                if self.state is State.SYNC:
                    return self._go(State.SYNC, best.id, now)
                return self._go(State.FOLLOWER, best.id, now)
            return None

        if self.state is State.SYNC:
            # The leader went away mid-sync: start again.
            if current is None or current.state is not State.LEADER:
                return self._go(State.LISTEN, None, now)
            return None

        # No visible leader. The leader is lost once it hasn't been heard for
        # long enough, or straight away if it's still there but has stopped leading.
        last = peers.get(self.leader)
        gone = (
            last is None
            or now - last.heard_at >= self.timing.leader_lost
            or (current is not None and current.state is not State.LEADER)
        )
        if not gone:
            return None
        if not self.lost_leader:
            # Say so in the heartbeat first, and give the other followers time
            # to see it, so they can compare coins rather than all taking over.
            self.lost_leader = True
            self.lost_since = now
            return None
        if now - self.lost_since < self.timing.listen_settle:
            return None
        rivals = [p for p in visible.values() if p.state is State.FOLLOWER and p.lost_leader]
        if self._wins(rivals):
            return self._lead_now(now)
        return None

    def _lead(self, visible: Mapping[str, PeerView], now: float) -> Change | None:
        if self.handover_to is not None:
            target = visible.get(self.handover_to)
            if target is not None and target.state is State.LEADER:
                return self._go(State.FOLLOWER, target.id, now)
            # Still waiting for the target to take over. It should, within a
            # heartbeat or two; if it's gone, the handover is cancelled.
            if target is None:
                self.handover_to = None
            return None

        # Leaders that are handing over aren't rivals.
        rivals = [p for p in visible.values() if p.state is State.LEADER and p.handover_to is None]
        if not rivals:
            return None
        # An established leader beats a newcomer outright; the coin only decides between equals.
        stronger = [p for p in rivals if p.established and not self.established]
        if stronger:
            best = max(stronger, key=lambda p: (p.coin, p.id))
            return self._go(State.FOLLOWER, best.id, now)
        equals = [p for p in rivals if p.established == self.established]
        if not equals:
            return None  # Every rival is a newcomer; they'll step down to this Virgil.
        wins = self._wins(equals)
        if wins is False:
            best = max(equals, key=lambda p: p.coin)
            return self._go(State.FOLLOWER, best.id, now)
        return None

    def _lead_now(self, now: float) -> Change:
        self.coin = self.draw()
        self.established = self.heard_group
        return self._go(State.LEADER, None, now)

    def _go(self, state: State, leader: str | None, now: float) -> Change:
        change = Change(self.state, state, self.leader, leader)
        self.state = state
        self.leader = leader
        self.since = now
        if state is not State.FOLLOWER:
            self.lost_leader = False
        if state is not State.LEADER:
            self.handover_to = None
        return change
