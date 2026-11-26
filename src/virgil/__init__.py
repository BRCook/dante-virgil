"""Virgil: PTPv2 clocking for Dante over wireless links."""


def _version() -> str:
    """The installed package's version, which comes from pyproject.toml: the one place it's set. Run from a
    checkout without installing (as the tests are), it's read from pyproject.toml directly."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("dante-virgil")
    except PackageNotFoundError:
        import re
        from pathlib import Path

        try:
            text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
            found = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            return found.group(1) if found else "unknown"
        except OSError:
            return "unknown"


__version__ = _version()
