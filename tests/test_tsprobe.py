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
    def __init__(self, clock, slots, hold=0.0001, hardware=True, min_gap=None, cores=16, counts=True, late_every=None):
        self.clock, self.slots, self.hold, self.hardware, self.min_gap = clock, slots, hold, hardware, min_gap
        self.cores = cores
        self.counts = counts  # whether the driver counts what it skips
        self.skipped = 0
        self.late_every = late_every  # every so often, a timestamp the card produced arrives too late to be seen
        self.given = 0
        self.busy = []  # when each occupied slot frees up
        self.pending = [0, 0]
        self.last = None

    def send(self, which):
        now = self.clock()
        gap, self.last = (None if self.last is None else now - self.last), now
        self.busy = [t for t in self.busy if t > now]
        if not self.hardware or len(self.busy) >= self.slots:
            self.skipped += self.hardware
            return  # skipped: no slot free
        if self.min_gap is not None and gap is not None and 0 < gap < self.min_gap:
            self.skipped += 1
            return  # frames too close together, but not together: the card (or CPU) falls behind
        self.busy.append(now + self.hold)
        self.pending[which] += 1

    def collect(self, which, wait):
        count, self.pending[which] = self.pending[which], 0
        return count

    @property
    def parallel(self):
        return self.cores

    def simultaneous(self, senders):
        """Every sender's request arrives at the same instant: only as many as there are free slots succeed."""
        now = self.clock()
        self.busy = [t for t in self.busy if t > now]
        got = min(senders, self.slots - len(self.busy)) if self.hardware else 0
        self.skipped += (senders - got) if self.hardware else 0
        self.busy += [now + self.hold] * got
        self.clock.sleep(0.05)  # the real probe waits for the timestamps before the next round
        seen = 0
        for _ in range(got):
            self.given += 1
            if not (self.late_every and self.given % self.late_every == 0):
                seen += 1  # the card produced it; whether the probe saw it in time is another matter
        return seen

    def dropped(self):
        return self.skipped if self.counts else None


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
    assert result.slots == 4 and result.senders == 10 and result.pairs_lost == 0 and result.margin == 100
    assert result.shareable


def test_more_slots_than_senders_is_at_least():
    """Ten senders on a card with 64 slots all get their timestamps: the card has at least ten."""
    result = run(slots=64)
    assert result.slots == 10 and result.senders == 10 and result.shareable


def test_fewer_cores_means_fewer_senders():
    result = run(slots=8, cores=4)  # only four senders can fire at once
    assert result.slots == 4 and result.senders == 4 and result.shareable


def test_falling_behind_at_100x_is_overload_not_slots():
    """Losses at 100 times the real rates that vanish at 50 times: the card is fine, just not that fast."""
    result = run(slots=4, min_gap=0.0006)  # at 100x, frames come as close as 0.42 ms; at 50x, 0.83 ms
    assert result.pairs_lost == 0 and result.margin == 50 and result.shareable
    assert not result.losses["100x"].startswith("0/") and result.losses["50x"].startswith("0/")


def test_a_card_that_cannot_keep_up_even_at_10x():
    result = run(slots=4, min_gap=0.005)  # at 10x, frames still come as close as 4.2 ms
    assert result.margin is None and not result.shareable and set(result.losses) == {"100x", "50x", "10x"}


def test_timestamps_the_probe_missed_arent_held_against_the_card():
    """Box A at boot: 3 of 100 timestamps missed, but the driver's counters show it dropped none."""
    result = run(slots=4, late_every=33)
    assert result.pairs_lost == 3 and result.pairs_dropped == 0
    assert result.contention_passed and result.shareable


def test_without_the_drivers_counters_every_loss_counts():
    result = run(slots=4, late_every=33, counts=False)
    assert result.pairs_lost == 3 and result.pairs_dropped is None and not result.shareable


def test_a_card_that_drops_timestamps_itself_is_still_caught():
    result = run(slots=1)
    assert result.pairs_dropped == result.pairs_lost == 50 and not result.shareable
