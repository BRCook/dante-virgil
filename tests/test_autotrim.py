import pytest

from virgil.autotrim import DEADBAND, MAX_STEP, MAX_TRIM, next_trim, unresponsive


def test_a_fast_leader_is_slowed_down():
    # Consoles at +4 ppm: the leader is faster than they'd like.
    assert next_trim(0, [4000]) == -MAX_STEP
    assert next_trim(0, [300]) == -90


def test_a_slow_leader_is_sped_up():
    assert next_trim(1000, [-1000]) == 1000 + MAX_STEP
    assert next_trim(1000, [-300]) == 1090


def test_the_average_of_every_console():
    assert next_trim(0, [600, -100]) == -75


def test_close_enough_is_left_alone():
    assert next_trim(500, [DEADBAND]) is None
    assert next_trim(500, [-DEADBAND, DEADBAND]) is None


def test_nothing_to_go_on():
    assert next_trim(500, []) is None
    assert next_trim(500, [5_000_000]) is None  # not a locked console


def test_the_trim_has_limits():
    assert next_trim(MAX_TRIM, [-50_000]) is None
    assert next_trim(MAX_TRIM - 100, [-50_000]) == MAX_TRIM


@pytest.mark.parametrize("response", [0.5, 1.0, 1.7, 2.4, 4.0, 6.0])
def test_converges_for_a_range_of_console_responses(response):
    """A console at +4.75 ppm with the trim at 0, responding by `response` ppm per ppm of trim."""
    natural = 4750
    trim = 0
    for _ in range(200):
        offset = natural + response * trim
        new = next_trim(trim, [round(offset)])
        if new is None:
            break
        trim = new
    assert abs(natural + response * trim) <= DEADBAND


def test_unresponsive():
    assert not unresponsive([])
    assert not unresponsive([(2000, 7350)])
    assert not unresponsive([(2000, 7350), (0, 5370)])  # the bench: 1:1
    assert not unresponsive([(2000, 7350), (1500, 7300)])  # not far enough to judge yet
    assert unresponsive([(0, 5370), (-500, 5379), (-1000, 5394), (-1500, 5379)])  # tonight's runaway


def test_followers_settled():
    from virgil.autotrim import followers_settled

    assert followers_settled([])  # no followers: nothing to wait for
    assert followers_settled([{"rms_offset_ns": 6089}])
    assert not followers_settled([{"rms_offset_ns": 6089}, {"rms_offset_ns": 45_000}])
    assert not followers_settled([None])  # a follower whose clock isn't known yet


def test_readings_are_averaged_once_every_device_has_five_minutes():
    from virgil.autotrim import ReadingHistory

    history = ReadingHistory()
    for t in range(0, 600, 5):
        readings = {"A/Compact": 2000, **({"B/Rack": -1000 if t % 10 else -3000} if t >= 200 else {})}
        history.add(float(t), readings, trim_ppb=0)
    both = {"A/Compact", "B/Rack"}
    assert history.averages(450.0, both, 0) == {}  # B/Rack only has 250 s of readings: wait for it
    averages = history.averages(595.0, both, 0)
    assert averages["A/Compact"] == 2000.0 and averages["B/Rack"] == pytest.approx(-2000.0, abs=40)


def test_readings_from_before_a_trim_change_are_corrected_for_it():
    """Lowering the trim by 100 ppb lowers the readings by about as much; earlier readings are shifted to match."""
    from virgil.autotrim import ReadingHistory

    history = ReadingHistory()
    for t in range(0, 600, 5):
        trim = 0 if t < 400 else -100
        if t == 400:
            history.trim_changed(400.0)
        history.add(float(t), {"A/Compact": 500 + trim}, trim_ppb=trim)
    assert history.averages(595.0, {"A/Compact"}, -100) == {"A/Compact": 400.0}  # all of them, as if at -100
    raw = history.averages(595.0, {"A/Compact"}, -100, corrected=False)["A/Compact"]
    assert 400.0 < raw < 500.0  # what they actually read, across the change


def run_wander(period: float, minutes: int = 60) -> tuple[int, int, int, int]:
    """One Wing steady at +2 ppm, the other at -2 ppm but wandering by a ppm with this period (as a device behind a
    follower does). Returns (old rule's steps, new rule's steps, lowest trim, highest trim)."""
    import math

    from virgil import autotrim

    history, trim, steps_new, steps_old, low, high = autotrim.ReadingHistory(), 0, 0, 0, 0, 0
    for t in range(0, minutes * 60, 5):
        readings = {"Box A/WingCompact": 2000 + trim,
                    "Box B/WingRack": round(-2000 + trim + 1000 * math.sin(2 * math.pi * t / period + 1))}
        history.add(float(t), readings, trim)
        if t and t % 60 == 0:
            steps_old += autotrim.next_trim(0, list(readings.values())) is not None
            averages = history.averages(float(t), readings, trim)
            if averages and (new := autotrim.next_trim(trim, [round(v) for v in averages.values()])) is not None:
                trim, steps_new = new, steps_new + 1
                history.trim_changed(float(t))
            low, high = min(low, trim), max(high, trim)
    return steps_old, steps_new, low, high


def test_a_wandering_device_doesnt_make_auto_trim_hunt():
    """Readings at any moment straddle the dead band; five-minute averages don't."""
    for period in (60, 120, 180, 300, 450):
        steps_old, steps_new, low, high = run_wander(period)
        assert steps_old > 30 and steps_new == 0, (period, steps_old, steps_new)


def test_a_slow_wander_only_nudges_the_trim():
    """Wandering over ten minutes or more, a five-minute average can't tell it from drift, so it partly follows
    it, but only within a few tenths of a ppm."""
    for period in (600, 900):
        _, _, low, high = run_wander(period)
        assert -300 <= low and high <= 300, (period, low, high)


def test_a_real_offset_is_still_steered_out():
    from virgil import autotrim

    history, trim = autotrim.ReadingHistory(), 0
    for t in range(0, 60 * 60, 5):
        readings = {"A/Compact": 3000 + trim, "B/Rack": 1000 + trim}  # averaging +2 ppm: the leader is slow
        history.add(float(t), readings, trim)
        if t and t % 60 == 0:
            averages = history.averages(float(t), readings, trim)
            if averages and (new := autotrim.next_trim(trim, [round(v) for v in averages.values()])) is not None:
                trim = new
                history.trim_changed(float(t))
    assert -2200 <= trim <= -1800  # steered to within the dead band of cancelling the +2 ppm
