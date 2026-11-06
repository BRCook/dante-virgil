from virgil.nic_profiles import ProfileStatus, profile_for


def test_known_drivers():
    assert profile_for("igc").status is ProfileStatus.TESTED
    assert profile_for("r8126").needs_hwstamp_ctl
    assert profile_for("r8126").hwstamp_ctl_args == ("-t", "1", "-r", "12")
    assert not profile_for("igc").needs_hwstamp_ctl
    assert profile_for("r8169").status is ProfileStatus.UNSUPPORTED


def test_unknown_driver():
    assert profile_for("e1000e") is None
    assert profile_for(None) is None
