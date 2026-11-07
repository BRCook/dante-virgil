import pytest

from virgil.autotrim import DEADBAND, MAX_STEP, MAX_TRIM, next_trim, unresponsive


def test_a_fast_leader_is_slowed_down():
    # Consoles at +4 ppm: the leader is faster than they'd like.
    assert next_trim(0, [4000]) == -MAX_STEP
    assert next_trim(0, [1000]) == -300


def test_a_slow_leader_is_sped_up():
    assert next_trim(1000, [-1000]) == 1300


def test_the_average_of_every_console():
    assert next_trim(0, [3000, -1000]) == -300


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
    assert not unresponsive([(2000, 7350), (1000, 7300)])  # not far enough to judge yet
    assert unresponsive([(0, 5370), (-500, 5379), (-1000, 5394), (-1500, 5379)])  # tonight's runaway
