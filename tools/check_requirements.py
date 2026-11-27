#!/usr/bin/env python3
"""Check that pyproject.toml, uv.lock and requirements.txt agree, so nothing stale gets installed.

The chain: pyproject.toml lists Virgil's dependencies; `uv lock` resolves them into uv.lock (recording the list
it was made from); `uv export` writes uv.lock's packages into requirements.txt, which is all install.sh reads.
A missed step anywhere shows up here as a disagreement, with the command that fixes it.

The build tools have a chain of their own: pyproject.toml's [build-system] pins the build backend's version,
build-requirements.in names the same, and build-requirements.txt pins it and its dependencies with hashes, so
building Virgil never fetches a build tool unchecked.

Uses only Python's standard library, so install.sh can run it on a Clock Box. The test suite runs it too.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FIX = "run: uv lock"
EXPORT_FIX = "run: uv export --format requirements-txt --no-dev --no-emit-project -o requirements.txt"
BUILD_FIX = ("run: uv pip compile build-requirements.in --generate-hashes --universal --python-version 3.13 "
             "-o build-requirements.txt")


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
    return found + build_problems(root, project)


def build_problems(root: Path, project: dict) -> list[str]:
    """The build tools: pyproject.toml's [build-system] against build-requirements.in and build-requirements.txt."""
    found = []
    pins = {}
    for text in project.get("build-system", {}).get("requires", []):
        name, specifier = dependency(text)
        if not specifier.startswith("==") or any(c in specifier[2:] for c in "<>=!~*,"):
            found.append(f"pyproject.toml's [build-system] must pin {name} exactly (as {name}==version), not {text!r}")
            continue
        pins[name] = specifier[2:]
    named = {}
    source = root / "build-requirements.in"
    if source.exists():
        for line in source.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                name, specifier = dependency(line)
                named[name] = specifier.removeprefix("==")
    if named != pins:
        found.append(f"build-requirements.in names {named}, not pyproject.toml's [build-system] {pins}: make them agree, "
                     f"then {BUILD_FIX}")
    listed, unhashed = {}, []
    path = root / "build-requirements.txt"
    text = path.read_text() if path.exists() else ""
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        if match := re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", block):
            listed[normalise(match.group(1))] = match.group(2)
            if "--hash=sha256:" not in block:
                unhashed.append(match.group(1))
    for name, version in sorted(pins.items()):
        if listed.get(name) != version:
            found.append(f"build-requirements.txt has {name} {listed.get(name)}, not pyproject.toml's {version}: {BUILD_FIX}")
    if unhashed:
        found.append(f"build-requirements.txt has packages without hashes ({', '.join(unhashed)}): {BUILD_FIX}")
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
