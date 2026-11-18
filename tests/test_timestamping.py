from virgil import timestamping
from virgil.ptpv1 import TX_HARDWARE, timestamping_flags

ETHTOOL_STATS = """NIC statistics:
     rx_packets: 1234
     tx_hwtstamp_timeouts: 0
     tx_hwtstamp_skipped: 3
     rx_hwtstamp_cleared: 0
     tx_lpi_counter: 7
"""


def test_slots_are_unknown_until_measured(tmp_path):
    slots = timestamping.slots_for("enp1s0", "igc", "6.12.0", tmp_path / "none.json")
    assert slots == timestamping.Slots(None, "unknown")  # no guessing from the driver
    assert slots.effective == 1 and not slots.can_share  # so assume the worst


def test_descriptions():
    assert timestamping.Slots(4, "probe").describe() == "4 transmit timestamp slots (probed)"
    assert timestamping.Slots(4, "probe", shareable=True, margin=50).describe() == (
        "4 transmit timestamp slots (probed; shared safely at 50× normal load)")
    assert timestamping.Slots(4, "probe", shareable=False).describe() == (
        "4 transmit timestamp slots (probed), but sharing lost timestamps under test")
    assert timestamping.Slots(0, "probe", shareable=False).describe() == "No hardware transmit timestamps (probed)"
    assert timestamping.Slots(1, "evidence").describe() == "1 transmit timestamp slot (seen losing timestamps)"
    assert timestamping.Slots(None, "unknown").describe() == "Transmit timestamp slots unknown, so treated as 1"


def test_evidence_survives_for_that_card_driver_and_kernel(tmp_path):
    path = tmp_path / "timestamping.json"
    timestamping.save_evidence(timestamping.card_key("enp1s0", "igc", "6.12.0"), "tx_hwtstamp_skipped rose", path)
    assert timestamping.slots_for("enp1s0", "igc", "6.12.0", path) == timestamping.Slots(1, "evidence")
    assert timestamping.slots_for("enp1s0", "igc", "6.13.0", path).source == "unknown"  # a new kernel: a fresh look


def test_watching_for_lost_timestamps():
    counters = timestamping.skipped_counters(ETHTOOL_STATS)
    assert counters == {"tx_hwtstamp_timeouts": 0, "tx_hwtstamp_skipped": 3}  # not rx, not unrelated counters
    watch = timestamping.Watch()
    assert watch.counters_rose(counters) is None  # the first reading is the baseline
    assert watch.counters_rose(counters) is None
    assert watch.counters_rose({**counters, "tx_hwtstamp_skipped": 4}) == "tx_hwtstamp_skipped rose"
    assert watch.ptp4l_timed_out("ptp4l[123]: timed out while polling for tx timestamp") is not None
    assert watch.ptp4l_timed_out("ptp4l[123]: selected best master clock") is None


def test_ptpv1_leaves_transmit_slots_alone_when_it_must():
    assert timestamping_flags(True) & TX_HARDWARE and not timestamping_flags(False) & TX_HARDWARE
    assert timestamping_flags(True) ^ timestamping_flags(False) == TX_HARDWARE  # nothing else changes


def test_a_count_the_probe_couldnt_exceed_is_at_least():
    slots = timestamping.Slots(10, "probe", shareable=True, margin=100, senders=10)
    assert slots.count_text == "10+" and slots.at_least
    assert slots.describe() == "At least 10 transmit timestamp slots (probed; shared safely at 100× normal load)"
    exact = timestamping.Slots(4, "probe", shareable=True, margin=100, senders=10)
    assert exact.count_text == "4" and exact.describe().startswith("4 transmit timestamp slots")
