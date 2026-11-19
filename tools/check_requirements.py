#!/usr/bin/env python3
"""Check that pyproject.toml, uv.lock and requirements.txt agree, so nothing stale gets installed.

The chain: pyproject.toml lists Virgil's dependencies; `uv lock` resolves them into uv.lock (recording the list
it was made from); `uv export` writes uv.lock's packages into requirements.txt, which is all install.sh reads.
A missed step anywhere shows up here as a disagreement, with the command that fixes it.

Uses only Python's standard library, so install.sh can run it on a Clock Box. The test suite runs it too.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FIX = "run: uv lock"
EXPORT_FIX = "run: uv export --format requirements-txt --no-dev --no-emit-project -o requirements.txt"


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency(text: str) -> tuple[str, str]:
    """'aiohttp>=3.9' becomes ('aiohttp', '>=3.9')."""
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*(.*?)\s*$", text)
    return normalise(match.group(1)), match.group(2).replace(" ", "")


def problems(root: Path = ROOT) -> list[str]:
    project = tomllib.loads((root / "pyproject.toml").read_text())
    packages = tomllib.loads((root / "uv.lock").read_text())["package"]
    own_name = normalise(project["project"]["name"])
    own = next(p for p in packages if normalise(p["name"]) == own_name)
    found = []

    # pyproject.toml against uv.lock: the lock records the dependency list it was made from.
    wanted = {dependency(d) for d in project["project"]["dependencies"]}
    sources = {normalise(name): sorted(s["url"] for s in (entries if isinstance(entries, list) else [entries]))
               for name, entries in project.get("tool", {}).get("uv", {}).get("sources", {}).items()}
    recorded = own["metadata"]["requires-dist"]
    specifiers = {(normalise(r["name"]), r["specifier"].replace(" ", "")) for r in recorded if "specifier" in r}
    urls: dict[str, list[str]] = {}
    for r in recorded:
        if "url" in r:
            urls.setdefault(normalise(r["name"]), []).append(r["url"])
    names = {normalise(r["name"]) for r in recorded}
    if names != {name for name, _ in wanted}:
        found.append(f"uv.lock depends on {sorted(names)}, not pyproject.toml's list: {LOCK_FIX}")
    for name, specifier in sorted(wanted):
        if name in sources and sorted(urls.get(name, [])) != sources[name]:
            found.append(f"uv.lock's {name} files aren't pyproject.toml's: {LOCK_FIX}")
        if specifier and (name, specifier) not in specifiers:
            found.append(f"uv.lock was made for another {name} than {specifier}: {LOCK_FIX}")

    # uv.lock against requirements.txt: everything Virgil needs, following each package's dependencies
    # through the lock (leaving out the development-only ones, as uv export --no-dev does).
    by_name: dict[str, list[dict]] = {}
    for package in packages:
        by_name.setdefault(normalise(package["name"]), []).append(package)
    needed: set[tuple[str, str]] = set()
    queue = [normalise(d["name"]) for d in own.get("dependencies", [])]
    while queue:
        name = queue.pop()
        for package in by_name.get(name, []):
            entry = (name, package["version"])
            if entry not in needed:
                needed.add(entry)
                queue += [normalise(d["name"]) for d in package.get("dependencies", [])]
    listed = set()
    for line in (root / "requirements.txt").read_text().splitlines():
        if match := re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line):
            listed.add((normalise(match.group(1)), match.group(2)))
        elif match := re.match(r"^([A-Za-z0-9_.-]+) @ \S*/[^/]+?-([0-9][^-/]*)-py3", line):
            listed.add((normalise(match.group(1)), match.group(2)))
    missing, extra = sorted(needed - listed), sorted(listed - needed)
    if missing or extra:
        found.append(f"requirements.txt is missing {missing} and has {extra} that uv.lock doesn't: {EXPORT_FIX}")
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
