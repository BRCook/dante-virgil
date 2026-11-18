import json
import os

import pytest

from virgil import interfaces as ifc
from virgil.files import UnsafeFileError
from virgil.generate import Role, Tuning
from virgil.interfaces import Interface, TimestampingInfo
from virgil.request import TimingRequest, to_json
from virgil.rootctl import PrepareError, hwstamp, prepare, sync, trim

HARDWARE = ifc.TX_HARDWARE | ifc.RX_HARDWARE | ifc.RAW_HARDWARE | ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE
SOFTWARE = ifc.TX_SOFTWARE | ifc.RX_SOFTWARE | ifc.SOFTWARE

INTERFACES = {
    "enp1s0": Interface("enp1s0", "aa:aa", "igc", True, True, TimestampingInfo(HARDWARE, 0, 3, 3)),
    "enp191s0": Interface("enp191s0", "bb:bb", "r8126", True, True, TimestampingInfo(HARDWARE, 1, 3, 1 << 12)),
    "enp5s0": Interface("enp5s0", "cc:cc", "r8169", True, True, TimestampingInfo(SOFTWARE, -1, 0, 0)),
    "br0": Interface("br0", "dd:dd", None, False, True, TimestampingInfo(SOFTWARE, -1, 0, 0)),
    "wlan0": Interface("wlan0", "ee:ee", "mt7921e", True, True, TimestampingInfo(0, -1, 0, 0)),
}


def lookup(name):
    return INTERFACES.get(name)


def _prepare(tmp_path, request: TimingRequest):
    request_path = tmp_path / "request.json"
    request_path.write_text(to_json(request))
    generated = tmp_path / "generated"
    prepare(request_path, generated, lookup)
    return generated


def _follower(interface="enp1s0", **changes):
    return TimingRequest(Role.FOLLOWER, interface, "192.168.20.21", ("192.168.20.21",), 22800, **changes)


def _leader(interface="enp1s0", **changes):
    return TimingRequest(Role.LEADER, interface, None, ("192.168.20.0/24",), 21500, **changes)


def test_follower_with_a_phc(tmp_path):
    generated = _prepare(tmp_path, _follower())
    assert "[enp1s0]" in (generated / "ptp4l.conf").read_text()
    assert "server 192.168.20.21 iburst" in (generated / "chrony.conf").read_text()
    assert (generated / "phc2sys.env").read_text() == (
        'PHC2SYS_ARGS="-s CLOCK_REALTIME -c /dev/ptp0 -O 0 -P 0.03 -I 0.0005 -S 0.001 -m -q"\n'
    )
    plan = json.loads((generated / "plan.json").read_text())
    assert plan["role"] == "follower"
    assert plan["hwstamp"] is None
    assert plan["sync"][0] == {
        "args": ["/usr/sbin/chronyd", "-q", "-t", "20", "server 192.168.20.21 iburst"],
        "may_fail": True,
    }
    assert plan["sync"][-2]["args"] == ["/usr/sbin/phc_ctl", "/dev/ptp0", "--", "freq", "22800"]
    assert plan["sync"][-1] == {"args": ["/usr/sbin/hwclock", "--systohc"], "may_fail": True}
    assert plan["phc"] == "/dev/ptp0"


def test_leader_has_no_phc2sys(tmp_path):
    generated = _prepare(tmp_path, _follower())
    assert (generated / "phc2sys.env").exists()
    request_path = tmp_path / "request.json"
    request_path.write_text(to_json(_leader()))
    prepare(request_path, generated, lookup)
    assert not (generated / "phc2sys.env").exists()
    assert "refclock PHC /dev/ptp0" in (generated / "chrony.conf").read_text()


def test_the_phc_comes_from_the_hardware(tmp_path):
    generated = _prepare(tmp_path, _follower("enp191s0"))
    assert "-c /dev/ptp1" in (generated / "phc2sys.env").read_text()


def test_nic_profile_applies_hwstamp_ctl(tmp_path):
    generated = _prepare(tmp_path, _follower("enp191s0"))
    plan = json.loads((generated / "plan.json").read_text())
    assert plan["hwstamp"] == ["/usr/sbin/hwstamp_ctl", "-i", "enp191s0", "-t", "1", "-r", "12"]


def test_nic_profile_can_be_declined(tmp_path):
    generated = _prepare(tmp_path, _follower("enp191s0", use_nic_profile=False))
    plan = json.loads((generated / "plan.json").read_text())
    assert plan["hwstamp"] is None


def test_software_timestamping(tmp_path):
    generated = _prepare(tmp_path, _follower("enp5s0"))
    assert "time_stamping           software" in (generated / "ptp4l.conf").read_text()
    assert not (generated / "phc2sys.env").exists()


def test_tuning_reaches_the_configs(tmp_path):
    generated = _prepare(tmp_path, _follower(tuning=Tuning(maxdelayquant=0.5, phc2sys_kp=0.1)))
    assert "maxdelayquant 0.5 " in (generated / "chrony.conf").read_text()
    assert "-P 0.1 " in (generated / "phc2sys.env").read_text()


@pytest.mark.parametrize("interface", ["eth9", "br0", "wlan0"])
def test_unusable_interfaces_are_refused(tmp_path, interface):
    with pytest.raises(PrepareError):
        _prepare(tmp_path, _follower(interface))


def test_a_symlinked_request_is_refused(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(to_json(_follower()))
    os.symlink(target, tmp_path / "request.json")
    with pytest.raises(UnsafeFileError):
        prepare(tmp_path / "request.json", tmp_path / "generated", lookup)


def test_an_oversized_request_is_refused(tmp_path):
    (tmp_path / "request.json").write_text(" " * 100_000)
    with pytest.raises(UnsafeFileError):
        prepare(tmp_path / "request.json", tmp_path / "generated", lookup)


def test_generated_files_are_world_readable(tmp_path):
    generated = _prepare(tmp_path, _follower())
    for path in generated.iterdir():
        assert path.stat().st_mode & 0o777 == 0o644


class Recorder:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, args):
        self.calls.append(list(args))
        return self.results.get(args[0], 0)


def test_sync_runs_the_plan_in_order(tmp_path):
    generated = _prepare(tmp_path, _follower("enp191s0"))
    run = Recorder()
    assert sync(generated, run) == 0
    assert [call[0] for call in run.calls] == [
        "/usr/sbin/chronyd", "/usr/sbin/hwstamp_ctl", "/usr/sbin/phc_ctl", "/usr/sbin/phc_ctl", "/usr/sbin/hwclock",
    ]


def test_sync_carries_on_when_the_leader_cant_be_reached(tmp_path):
    generated = _prepare(tmp_path, _follower())
    run = Recorder({"/usr/sbin/chronyd": 1})
    assert sync(generated, run) == 0
    assert len(run.calls) == 4  # chronyd, the PHC's time and rate, and the hardware clock


def test_sync_stops_when_the_phc_cant_be_set(tmp_path):
    generated = _prepare(tmp_path, _follower())
    run = Recorder({"/usr/sbin/phc_ctl": 2})
    assert sync(generated, run) == 2
    assert run.calls[-1] == ["/usr/sbin/phc_ctl", "/dev/ptp0", "--", "set"]


def test_hwstamp(tmp_path):
    run = Recorder()
    assert hwstamp(_prepare(tmp_path, _follower("enp191s0")), run) == 0
    assert run.calls == [["/usr/sbin/hwstamp_ctl", "-i", "enp191s0", "-t", "1", "-r", "12"]]
    run = Recorder()
    assert hwstamp(_prepare(tmp_path, _follower("enp1s0")), run) == 0
    assert run.calls == []


def test_an_unreadable_request_is_a_permission_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = tmp_path / "request.json"
    path.write_text(to_json(_follower()))
    path.chmod(0)
    with pytest.raises(PermissionError):
        prepare(path, tmp_path / "generated", lookup)


def test_trim(tmp_path):
    run = Recorder()
    assert trim(_prepare(tmp_path, _leader()), run) == 0
    assert run.calls == [["/usr/sbin/phc_ctl", "/dev/ptp0", "--", "freq", "21500"]]
    run = Recorder()
    assert trim(_prepare(tmp_path, _leader("enp5s0")), run) == 0  # software timestamping: no PHC
    assert run.calls == []


def test_ntp_hardware_timestamps_only_on_hardware(tmp_path):
    generated = _prepare(tmp_path, _follower(ntp_hwtimestamp="enp1s0"))
    assert "hwtimestamp enp1s0 rxfilter all" in (generated / "chrony.conf").read_text()
    generated = _prepare(tmp_path, _follower(ntp_hwtimestamp="enp5s0"))  # software only: ignored
    assert "hwtimestamp" not in (generated / "chrony.conf").read_text()


def no_commands(args, **kwargs):
    import subprocess as sp

    return sp.CompletedProcess(args, 0, "[]", "")


@pytest.fixture(autouse=True)
def nothing_real(monkeypatch, tmp_path):
    """No virgil-root test may touch the real system, even one that forgets an argument."""
    import virgil.rootctl as rootctl

    def refuse(args, **kwargs):
        raise AssertionError(f"a test tried to run a real command: {args}")

    monkeypatch.setattr(rootctl, "SYSTEM_RUN", refuse)
    monkeypatch.setattr(rootctl, "DHCPCD_CONF", tmp_path / "guard" / "dhcpcd.conf")
    monkeypatch.setattr(rootctl, "INTERFACES_FILE", tmp_path / "guard" / "interfaces")
    monkeypatch.setattr(rootctl, "DHCP_NAMES_REQUEST", tmp_path / "guard" / "dhcp-names.json")


def test_dhcpnames(tmp_path):
    from tests.test_dhcpnames import BOX
    from virgil.rootctl import dhcpnames

    interfaces = tmp_path / "interfaces"
    interfaces.write_text(BOX)
    request = tmp_path / "dhcp-names.json"
    request.write_text(json.dumps({"ptp_interface": "enp1s0", "mgmt_interface": "enp5s0"}))
    conf = tmp_path / "dhcpcd.conf"
    assert dhcpnames(request, interfaces, "debian-server-1", conf, no_commands) == 0
    assert "hostname debian-server-1-ptp" in interfaces.read_text()
    assert (tmp_path / "interfaces.virgil-backup").read_text() == BOX
    request.write_text(json.dumps({"ptp_interface": "enp1s0; reboot", "mgmt_interface": None}))
    assert dhcpnames(request, interfaces, "x", conf, no_commands) == 1
    assert dhcpnames(request, tmp_path / "missing", "x", conf, no_commands) == 0  # no ifupdown: nothing to do


def test_dhcpnames_also_handles_the_default_route(tmp_path):
    import subprocess as sp

    from tests.test_dhcpnames import BOX, DHCPCD
    from virgil.rootctl import dhcpnames

    (tmp_path / "interfaces").write_text(BOX)
    (tmp_path / "dhcpcd.conf").write_text(DHCPCD)
    request = tmp_path / "dhcp-names.json"
    request.write_text(json.dumps({"ptp_interface": "enp1s0", "mgmt_interface": "enp5s0"}))
    ran = []

    def run(args, **kwargs):
        ran.append(args)
        routes = [{"dst": "default", "dev": "enp1s0"}, {"dst": "default", "dev": "enp5s0"}]
        return sp.CompletedProcess(args, 0, json.dumps(routes), "")

    assert dhcpnames(request, tmp_path / "interfaces", "box", tmp_path / "dhcpcd.conf", run) == 0
    assert "interface enp1s0\nnogateway" in (tmp_path / "dhcpcd.conf").read_text()
    assert (tmp_path / "dhcpcd.conf.virgil-backup").read_text() == DHCPCD
    assert ["ip", "-4", "route", "del", "default", "dev", "enp1s0"] in ran


def test_the_only_default_route_is_left_alone(tmp_path):
    import subprocess as sp

    from virgil.rootctl import drop_ptp_default_route

    ran = []

    def run(args, **kwargs):
        ran.append(args)
        return sp.CompletedProcess(args, 0, json.dumps([{"dst": "default", "dev": "enp1s0"}]), "")

    drop_ptp_default_route("enp1s0", run)
    assert not any("del" in a for a in ran)


def test_dhcpnames_adds_a_missing_stanza_and_brings_it_up(tmp_path):
    import subprocess as sp

    from tests.test_dhcpnames import BOX
    from virgil.rootctl import dhcpnames

    box_b = BOX.split("allow-hotplug enp1s0")[0].rstrip() + "\n"
    (tmp_path / "interfaces").write_text(box_b)
    request = tmp_path / "dhcp-names.json"
    request.write_text(json.dumps({"ptp_interface": "enp1s0", "mgmt_interface": "enp5s0", "add": ["enp1s0"]}))
    ran = []

    def run(args, **kwargs):
        ran.append(args)
        return sp.CompletedProcess(args, 0, "[]", "")

    assert dhcpnames(request, tmp_path / "interfaces", "debian-server-2", tmp_path / "dhcpcd.conf", run) == 0
    text = (tmp_path / "interfaces").read_text()
    assert "iface enp1s0 inet dhcp\n    # Set by Virgil\n    hostname debian-server-2-ptp" in text
    assert ["ifup", "enp1s0"] in ran
    # Asked again: already there, so nothing more is added or brought up.
    ran.clear()
    assert dhcpnames(request, tmp_path / "interfaces", "debian-server-2", tmp_path / "dhcpcd.conf", run) == 0
    assert (tmp_path / "interfaces").read_text().count("iface enp1s0") == 1 and ["ifup", "enp1s0"] not in ran


def test_settime_moves_the_clock_and_the_phc_with_it(tmp_path):
    from virgil.rootctl import settime

    generated = _prepare(tmp_path, _leader())
    request = tmp_path / "settime.json"
    request.write_text(json.dumps({"delta": -192.5}))
    moved, run = [], Recorder()
    assert settime(request, generated, run, set_clock=moved.append, now=lambda: 1_000_000.0) == 0
    assert moved == [999_807.5]  # 3 min 12.5 s back
    assert run.calls == [["/usr/sbin/phc_ctl", "/dev/ptp0", "--", "set"], ["/usr/sbin/hwclock", "--systohc"]]
    assert not request.exists()  # one use only
    assert settime(request, generated, run, set_clock=moved.append) == 1  # nothing left to apply


@pytest.mark.parametrize("delta", ["soon", None, True, float("inf"), 25 * 365 * 86400])
def test_settime_refuses_nonsense(tmp_path, delta):
    from virgil.rootctl import settime

    generated = _prepare(tmp_path, _leader())
    request = tmp_path / "settime.json"
    request.write_text(json.dumps({"delta": delta}))
    moved = []
    assert settime(request, generated, Recorder(), set_clock=moved.append) == 1 and moved == []


def test_the_plan_probes_a_hardware_interface_unless_told_not_to(tmp_path):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    plan = json.loads((_prepare(tmp_path / "on", _leader()) / "plan.json").read_text())
    assert plan["probe"] == {"interface": "enp1s0", "hwstamp": ["-t", "1", "-r", "1"]}
    plan = json.loads((_prepare(tmp_path / "off", _leader(probe_timestamps=False)) / "plan.json").read_text())
    assert plan["probe"] is None


def test_the_probe_runs_once_per_card_and_never_fails_the_sync(tmp_path):
    from virgil import timestamping, tsprobe
    from tests.test_tsprobe import Clock, SimulatedCard

    path = tmp_path / "timestamping-probe.json"
    clock = Clock()
    cards = []

    class Card(SimulatedCard):
        def __init__(self):
            super().__init__(clock, slots=4)
            cards.append(self)

    # The probe uses the real clock; a simulated card that answers instantly keeps it quick.
    import virgil.tsprobe as module

    original = module.probe
    module_probe = lambda card: original(card, clock=clock, sleep=clock.sleep)
    try:
        module.probe = module_probe
        from virgil.rootctl import probe_slots

        probe_slots({"interface": "enp1s0", "hwstamp": ["-t", "1"]}, Recorder(), Card(), path)
        probe_slots({"interface": "enp1s0", "hwstamp": ["-t", "1"]}, Recorder(), Card(), path)  # cached: skipped
    finally:
        module.probe = original
    assert len(cards) == 2 and cards[1].pending == [0, 0] and cards[1].last is None  # the second never sent a frame
    saved = timestamping.load_probes(path)
    assert len(saved) == 1 and next(iter(saved.values()))["slots"] == 4
    slots = timestamping.slots_for("enp1s0", None, probe_path=path, path=tmp_path / "no-evidence.json")
    assert slots.source == "probe" and slots.count == 4 and slots.can_share and slots.margin == 100


def test_a_probe_that_cannot_run_leaves_the_card_unmeasured(tmp_path):
    from virgil import timestamping
    from virgil.rootctl import probe_slots

    class Broken:
        def send(self, which):
            raise OSError("no such device")

        def close(self):
            pass

    probe_slots({"interface": "enp1s0"}, Recorder(), Broken(), tmp_path / "probe.json")
    assert timestamping.load_probes(tmp_path / "probe.json") == {}


def test_evidence_beats_the_probe(tmp_path):
    from virgil import timestamping

    key = timestamping.card_key("enp1s0", "igc")
    timestamping.save_probe(key, {"hardware": True, "slots": 4, "shareable": True, "margin": 100}, tmp_path / "p.json")
    timestamping.save_evidence(key, "tx_hwtstamp_skipped rose", tmp_path / "e.json")
    slots = timestamping.slots_for("enp1s0", "igc", path=tmp_path / "e.json", probe_path=tmp_path / "p.json")
    assert slots == timestamping.Slots(1, "evidence") and not slots.can_share
