"""The probe, against simulated cards: the slots, how long each timestamp is held, and how fast the card can go."""

from virgil import tsprobe


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.0)


class SimulatedCard(tsprobe.Card):
    def __init__(self, clock, slots, hold=0.0001, hardware=True, min_gap=None):
        self.clock, self.slots, self.hold, self.hardware, self.min_gap = clock, slots, hold, hardware, min_gap
        self.busy = []  # when each occupied slot frees up
        self.pending = [0, 0]
        self.last = None

    def send(self, which):
        now = self.clock()
        gap, self.last = (None if self.last is None else now - self.last), now
        self.busy = [t for t in self.busy if t > now]
        if not self.hardware or len(self.busy) >= self.slots:
            return  # skipped: no slot free
        if self.min_gap is not None and gap is not None and 0 < gap < self.min_gap:
            return  # frames too close together, but not together: the card (or CPU) falls behind
        self.busy.append(now + self.hold)
        self.pending[which] += 1

    def collect(self, which, wait):
        count, self.pending[which] = self.pending[which], 0
        return count


def run(**card):
    clock = Clock()
    return tsprobe.probe(SimulatedCard(clock, **card), clock=clock, sleep=clock.sleep)


def test_a_card_without_hardware_timestamps():
    result = run(slots=4, hardware=False)
    assert not result.hardware and result.slots is None and not result.shareable


def test_a_card_with_one_slot_loses_every_pair():
    result = run(slots=1)
    assert result.slots == 1 and result.pairs_lost == tsprobe.PAIRS
    assert result.margin is None and result.losses == {}  # no load test: contention already failed
    assert not result.shareable


def test_a_card_with_four_slots_shares():
    result = run(slots=4)
    assert result.slots == 4 and result.pairs_lost == 0 and result.margin == 100 and result.shareable


def test_falling_behind_at_100x_is_overload_not_slots():
    """Losses at 100 times the real rates that vanish at 50 times: the card is fine, just not that fast."""
    result = run(slots=4, min_gap=0.0006)  # at 100x, frames come as close as 0.42 ms; at 50x, 0.83 ms
    assert result.pairs_lost == 0 and result.margin == 50 and result.shareable
    assert not result.losses["100x"].startswith("0/") and result.losses["50x"].startswith("0/")


def test_a_card_that_cannot_keep_up_even_at_10x():
    result = run(slots=4, min_gap=0.005)  # at 10x, frames still come as close as 4.2 ms
    assert result.margin is None and not result.shareable and set(result.losses) == {"100x", "50x", "10x"}


def test_a_slot_held_longer_than_the_pairs_are_apart():
    """Two slots, but each held for longer than the gap between pairs: the second pair's timestamps find them full."""
    result = run(slots=2, hold=0.05)
    assert result.slots == 2 and result.pairs_lost > 0 and not result.shareable
