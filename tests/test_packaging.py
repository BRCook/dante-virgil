"""Packaging: pyproject.toml, uv.lock and requirements.txt must agree, or a Clock Box quietly installs something
stale; and the Debian package must match install.sh, and pin what it installs."""

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_requirements", ROOT / "tools" / "check_requirements.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def copy(tmp_path):
    for name in ("pyproject.toml", "uv.lock", "requirements.txt", "build-requirements.in", "build-requirements.txt"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def test_the_three_files_agree():
    assert check.problems() == []


def test_a_missed_export_is_caught(tmp_path):
    root = copy(tmp_path)
    text = (root / "requirements.txt").read_text()
    version = check.re.search(r"/netaudio-([0-9.]+)-py3", text).group(1)
    (root / "requirements.txt").write_text(text.replace(f"netaudio-{version}-", "netaudio-0.0.1-"))
    found = check.problems(root)
    assert len(found) == 1 and "uv export" in found[0] and "('netaudio', '0.0.1')" in found[0]


def test_a_missed_lock_is_caught(tmp_path):
    root = copy(tmp_path)
    text = (root / "pyproject.toml").read_text()
    (root / "pyproject.toml").write_text(text.replace('"aiohttp>=3.9"', '"aiohttp>=3.10"'))
    found = check.problems(root)
    assert found and all("uv lock" in problem for problem in found)


def test_virgils_version_comes_from_pyproject():
    """Set in one place, so a release can't report one version and package another."""
    import re
    from pathlib import Path

    import virgil

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert virgil.__version__ == re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1)


PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def test_wheels_are_pinned_by_their_hashes(tmp_path):
    import hashlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("pin_wheels", PACKAGING / "pin_wheels.py")
    pin_wheels = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin_wheels)
    (tmp_path / "aiohttp-3.14.5-cp313-cp313-manylinux_2_28_x86_64.whl").write_bytes(b"one")
    (tmp_path / "netaudio-2.1.1-py3-none-manylinux_2_28_x86_64.whl").write_bytes(b"two")
    (tmp_path / "dante_virgil-1.0.0-py3-none-any.whl").write_bytes(b"three")
    pins = pin_wheels.pins(tmp_path, excluding="dante-virgil")
    assert pins == [f"aiohttp==3.14.5 --hash=sha256:{hashlib.sha256(b'one').hexdigest()}",
                    f"netaudio==2.1.1 --hash=sha256:{hashlib.sha256(b'two').hexdigest()}"]
    assert pin_wheels.pins(tmp_path, only="dante-virgil")[0].startswith("dante-virgil==1.0.0 --hash=sha256:")
    (tmp_path / "netaudio-2.0.6-py3-none-manylinux_2_28_x86_64.whl").write_bytes(b"old")
    with pytest.raises(SystemExit, match="more than one wheel"):
        pin_wheels.pins(tmp_path)


def test_the_packages_scripts_parse():
    import subprocess

    for script in ("debian/postinst", "debian/prerm", "debian/postrm"):
        assert subprocess.run(["sh", "-n", str(PACKAGING / script)]).returncode == 0, script
    for script in ("build.sh", "install-offline.sh"):
        assert subprocess.run(["bash", "-n", str(PACKAGING / script)]).returncode == 0, script


def test_the_package_needs_what_install_sh_installs():
    """Two ways to install Virgil: they mustn't drift apart."""
    import re

    repo = PACKAGING.parent
    listed = re.search(r"^PACKAGES=\(([^)]*)\)", (repo / "install.sh").read_text(), re.MULTILINE).group(1).split()
    depends = re.search(r"^Depends: (.*)$", (PACKAGING / "debian" / "control").read_text(), re.MULTILINE).group(1)
    needed = {re.sub(r"\s*\(.*\)", "", item).strip() for item in depends.split(",")}
    assert set(listed) <= needed | {"python3-venv"} and needed - {"python3"} <= set(listed)


def test_every_unit_goes_into_the_package():
    """build.sh installs system/systemd/*.service, and postinst and postrm name units only by that pattern."""
    repo = PACKAGING.parent
    units = {p.name for p in (repo / "system" / "systemd").glob("*.service")}
    assert "dante-virgil.service" in units and all(u.startswith("dante-virgil") for u in units)
    assert 'install -m 644 "$REPO"/system/systemd/*.service' in (PACKAGING / "build.sh").read_text()


def test_netaudio_runs_as_its_own_user_everywhere():
    """netaudio parses Dante devices' traffic, so it runs as a user that can't read Virgil's secrets."""
    repo = PACKAGING.parent
    unit = (repo / "system" / "systemd" / "dante-virgil-netaudio.service").read_text()
    assert "User=dante-virgil-netaudio" in unit and "Group=dante-virgil-netaudio" in unit
    assert "HOME=/var/lib/dante-virgil-netaudio" in unit and "StateDirectory=dante-virgil-netaudio" in unit
    for installer in (repo / "install.sh", PACKAGING / "debian" / "postinst"):
        text = installer.read_text()
        assert "NETAUDIO_USER=dante-virgil-netaudio" in text and 'useradd' in text and ".local/share/netaudio" in text
    for purge in (repo / "uninstall.sh", PACKAGING / "debian" / "postrm"):
        text = purge.read_text()
        assert "userdel dante-virgil-netaudio" in text and "/var/lib/dante-virgil-netaudio" in text


def test_the_build_tools_are_pinned_and_checked(tmp_path):
    """Building Virgil never fetches a build tool unchecked (item 5 of the 2.0 plan)."""
    copy(tmp_path)
    assert check.problems(tmp_path) == []
    project = (tmp_path / "pyproject.toml").read_text()
    # An unpinned build backend.
    (tmp_path / "pyproject.toml").write_text(project.replace('requires = ["hatchling==1.33.0"]', 'requires = ["hatchling"]'))
    assert any("must pin hatchling exactly" in p for p in check.problems(tmp_path))
    # A newer one in pyproject.toml, without recompiling.
    (tmp_path / "pyproject.toml").write_text(project.replace("hatchling==1.33.0", "hatchling==1.34.0"))
    (tmp_path / "build-requirements.in").write_text("hatchling==1.34.0\n")
    assert any("build-requirements.txt has hatchling 1.33.0" in p for p in check.problems(tmp_path))
    # A package without its hash.
    (tmp_path / "pyproject.toml").write_text(project)
    (tmp_path / "build-requirements.in").write_text("hatchling==1.33.0\n")
    pinned = (tmp_path / "build-requirements.txt").read_text()
    stripped = re.sub(r"(pluggy==[^\n]*\\\n)(\s+--hash=sha256:[0-9a-f]+(\s*\\)?\n)+", r"\1", pinned)
    stripped = stripped.replace("pluggy==1.7.0 ; ", "pluggy==1.7.0 ; ").replace("\\\npathspec", "\npathspec")
    (tmp_path / "build-requirements.txt").write_text(stripped)
    assert any("without hashes (pluggy)" in p for p in check.problems(tmp_path))
