import asyncio

from virgil import clocks
from virgil.clocks import parse_phc2sys, parse_tracking

FOLLOWER = """Reference ID    : 0A0A64FD (10.10.100.253)
Stratum         : 2
Ref time (UTC)  : Fri Nov 06 04:12:51 2026
System time     : 0.000000307 seconds fast of NTP time
Last offset     : -0.000001854 seconds
RMS offset      : 0.000000079 seconds
Frequency       : 3.305 ppm fast
Residual freq   : +0.000 ppm
Skew            : 0.010 ppm
Root delay      : 0.000202041 seconds
Root dispersion : 0.000015342 seconds
Update interval : 1.0 seconds
Leap status     : Normal
"""

LEADER = """Reference ID    : 50484330 (PHC0)
Stratum         : 1
Ref time (UTC)  : Fri Nov 06 02:56:51 2026
System time     : 0.000000012 seconds slow of NTP time
Last offset     : +0.000000017 seconds
RMS offset      : 0.000000099 seconds
Frequency       : 13.590 ppm slow
Residual freq   : +0.000 ppm
Skew            : 0.000 ppm
Root delay      : 0.000000001 seconds
Root dispersion : 0.000000000 seconds
Update interval : 0.2 seconds
Leap status     : Normal
"""

PHC2SYS = """phc2sys[91722.390]: /dev/ptp0 sys offset     -1997 s2 freq   -1499 delay   3139
phc2sys[91723.391]: /dev/ptp0 sys offset     -2176 s2 freq   -1505 delay   3148
phc2sys[91724.391]: /dev/ptp0 sys offset     -2253 s2 freq   -1509 delay   3055
"""


def test_follower_tracking():
    assert parse_tracking(FOLLOWER) == {
        "source": "10.10.100.253",
        "system_offset_ns": 307,
        "frequency_ppm": 3.305,
        "last_offset_ns": -1854,
        "rms_offset_ns": 79,
        "skew_ppm": 0.01,
        "synchronised": True,
    }


def test_leader_tracking():
    tracking = parse_tracking(LEADER)
    assert tracking["source"] == "PHC0"
    assert tracking["system_offset_ns"] == -12 and tracking["frequency_ppm"] == -13.59


def test_not_tracking():
    assert parse_tracking("506 Cannot talk to daemon") is None
    unsynchronised = FOLLOWER.replace("Leap status     : Normal", "Leap status     : Not synchronised")
    assert parse_tracking(unsynchronised)["synchronised"] is False


def test_phc2sys_takes_the_latest_report():
    assert parse_phc2sys(PHC2SYS) == {
        "phc_offset_ns": -2253,
        "phc_locked": True,
        "phc_adjustment_ppm": -1.509,
        "phc_read_delay_ns": 3055,
    }
    assert parse_phc2sys("/dev/ptp0 sys offset 4000 s0 freq +0 delay 3000")["phc_locked"] is False
    assert parse_phc2sys("nothing here") is None


def test_read_clock(monkeypatch):
    outputs = {"chronyc": FOLLOWER, "journalctl": PHC2SYS}

    async def output(*args, timeout=3.0):
        return outputs.get(args[0])

    monkeypatch.setattr(clocks, "_output", output)
    follower = asyncio.run(clocks.read_clock(True, None))
    assert follower["source"] == "10.10.100.253" and follower["phc_offset_ns"] == -2253
    outputs["chronyc"] = LEADER
    leader = asyncio.run(clocks.read_clock(False, 21500))
    assert leader["phc_trim_ppm"] == 21.5 and "phc_offset_ns" not in leader


def test_missing_commands_give_nothing():
    async def scenario():
        return await clocks._output("no-such-command-anywhere")

    assert asyncio.run(scenario()) is None
