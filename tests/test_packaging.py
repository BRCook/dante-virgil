"""pyproject.toml, uv.lock and requirements.txt must agree, or a Clock Box quietly installs something stale."""

import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_requirements", ROOT / "tools" / "check_requirements.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def copy(tmp_path):
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
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
