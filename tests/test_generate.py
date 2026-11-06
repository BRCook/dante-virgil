"""The generated configuration must match the prototype's reference build."""

import pytest

from virgil.generate import (
    Command,
    Role,
    TimingSetup,
    Tuning,
    chrony_conf,
    hwstamp_ctl_args,
    phc2sys_args,
    ptp4l_conf,
    sync_commands,
)
from virgil.nic_profiles import profile_for

I226 = TimingSetup(ptp_interface="enp1s0", phc="/dev/ptp0", profile=profile_for("igc"), trim_ppb=21500)
RTL8126 = TimingSetup(ptp_interface="enp191s0", phc="/dev/ptp0", profile=profile_for("r8126"), trim_ppb=22800)
SOFTWARE = TimingSetup(ptp_interface="enp5s0", phc=None, profile=profile_for("r8169"))


def _settings(text: str) -> list[str]:
    """The non-blank, non-comment lines, with runs of spaces collapsed."""
    return [" ".join(line.split()) for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_ptp4l_matches_the_reference_build():
    assert _settings(ptp4l_conf(RTL8126)) == [
        "[global]",
        "domainNumber 0",
        "priority1 1",
        "serverOnly 1",
        "network_transport UDPv4",
        "ptp_minor_version 0",
        "logSyncInterval -3",
        "logAnnounceInterval 1",
        "logMinDelayReqInterval -3",
        "tx_timestamp_timeout 50",
        "[enp191s0]",
    ]


def test_ptp4l_software_timestamping():
    assert "time_stamping software" in _settings(ptp4l_conf(SOFTWARE))
    assert "time_stamping software" not in _settings(ptp4l_conf(I226))


def test_chrony_leader():
    settings = _settings(chrony_conf(Role.LEADER, I226, allow=["192.168.20.0/24"]))
    assert "local stratum 1" in settings
    assert "allow 192.168.20.0/24" in settings
    assert "refclock PHC /dev/ptp0 poll 0 dpoll -2 prefer" in settings
    assert not any(line.startswith("server ") for line in settings)


def test_chrony_leader_with_software_timestamping_has_no_refclock():
    settings = _settings(chrony_conf(Role.LEADER, SOFTWARE, allow=["192.168.20.0/24"]))
    assert "local stratum 1" in settings
    assert not any(line.startswith("refclock") for line in settings)


def test_chrony_follower_matches_the_live_show():
    settings = _settings(chrony_conf(Role.FOLLOWER, I226, allow=["192.168.20.21"], leader="192.168.20.21"))
    assert (
        "server 192.168.20.21 iburst minpoll 0 maxpoll 0 xleave maxdelayquant 0.25 minsamples 64 maxsamples 256"
        in settings
    )
    for line in ("authselectmode ignore", "corrtimeratio 20", "maxslewrate 10", "makestep 0.001 -1",
                 "allow 192.168.20.21"):
        assert line in settings
    assert not any(line.startswith(("local", "refclock")) for line in settings)


def test_chrony_follower_needs_a_leader():
    with pytest.raises(ValueError):
        chrony_conf(Role.FOLLOWER, I226, allow=[])


def test_tuning_is_applied():
    tuning = Tuning(maxdelayquant=0.5, phc2sys_kp=0.1)
    text = chrony_conf(Role.FOLLOWER, I226, allow=[], leader="10.0.0.1", tuning=tuning)
    assert "maxdelayquant 0.5 " in text
    assert phc2sys_args(I226, tuning)[phc2sys_args(I226, tuning).index("-P") + 1] == "0.1"


def test_phc2sys_matches_the_reference_build():
    assert phc2sys_args(RTL8126) == [
        "-s", "CLOCK_REALTIME", "-c", "/dev/ptp0", "-O", "0", "-P", "0.03", "-I", "0.0005", "-S", "0.001", "-m", "-q",
    ]


def test_no_phc2sys_without_a_phc():
    assert phc2sys_args(SOFTWARE) is None


def test_hwstamp_ctl_only_where_the_driver_needs_it():
    assert hwstamp_ctl_args(RTL8126) == ["-i", "enp191s0", "-t", "1", "-r", "12"]
    assert hwstamp_ctl_args(I226) is None
    assert hwstamp_ctl_args(SOFTWARE) is None


def test_sync_on_the_r8126_matches_the_reference_build():
    assert sync_commands(RTL8126, leader="192.168.20.21") == [
        Command(("/usr/sbin/chronyd", "-q", "-t", "20", "server 192.168.20.21 iburst"), may_fail=True),
        Command(("/usr/sbin/hwstamp_ctl", "-i", "enp191s0", "-t", "1", "-r", "12")),
        Command(("/usr/sbin/phc_ctl", "/dev/ptp0", "set")),
        Command(("/usr/sbin/phc_ctl", "/dev/ptp0", "freq", "22800")),
    ]


def test_sync_without_a_leader_keeps_its_own_clock():
    assert sync_commands(I226, leader=None) == [
        Command(("/usr/sbin/phc_ctl", "/dev/ptp0", "set")),
        Command(("/usr/sbin/phc_ctl", "/dev/ptp0", "freq", "21500")),
    ]


def test_sync_with_software_timestamping_only_takes_the_time():
    assert sync_commands(SOFTWARE, leader="10.0.0.1") == [
        Command(("/usr/sbin/chronyd", "-q", "-t", "20", "server 10.0.0.1 iburst"), may_fail=True),
    ]
    assert sync_commands(SOFTWARE, leader=None) == []
