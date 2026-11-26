#!/usr/bin/env python3
"""Pin a folder of wheels, for installing offline with pip's hash checking.

    pin_wheels.py <folder> [--only NAME | --except NAME]

Prints one line for each wheel: "name==version --hash=sha256:...", as pip's
--require-hashes wants. The package installs Virgil's dependencies from these, with
--no-index and --find-links pointing at the folder, so nothing is fetched and every
file is checked. requirements.txt can't be used for that directly: netaudio comes
from a URL there, which pip would try to fetch even with --no-index.
"""

import hashlib
import re
import sys
from pathlib import Path

# A wheel's file name: name-version(-build)?-python-abi-platform.whl (PEP 427).
WHEEL = re.compile(r"^(?P<name>[^-]+)-(?P<version>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(folder: Path, only: str | None = None, excluding: str | None = None) -> list[str]:
    lines = []
    for wheel in sorted(folder.glob("*.whl")):
        found = WHEEL.match(wheel.name)
        if not found:
            raise SystemExit(f"pin_wheels.py: {wheel.name} isn't named like a wheel")
        name = canonical(found["name"])
        if (only and name != canonical(only)) or (excluding and name == canonical(excluding)):
            continue
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lines.append(f"{name}=={found['version']} --hash=sha256:{digest}")
    names = [line.split("==")[0] for line in lines]
    if len(names) != len(set(names)):
        raise SystemExit("pin_wheels.py: more than one wheel for the same package")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] not in ("--only", "--except")):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    only = argv[2] if len(argv) == 3 and argv[1] == "--only" else None
    excluding = argv[2] if len(argv) == 3 and argv[1] == "--except" else None
    lines = pins(Path(argv[0]), only, excluding)
    if not lines:
        print("pin_wheels.py: no wheels to pin", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
