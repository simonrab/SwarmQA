#!/usr/bin/env python3
"""Validate bugs.json against the PlantedBugs Swift sources.

Checks that every bug has the required fields with valid values, that each
`kind` maps to its `category` the way `swarmqa.models.category_for_kind` does,
and that every accessibility identifier (the bug's own and each step in its
`path`) appears in the Swift sources. Stdlib only.

Usage: python3 check_bugs.py [path/to/bugs.json]
Exits 0 when everything checks out, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "PlantedBugs"

CATEGORIES = {"broken", "visual", "confusing", "crash"}
PLATFORMS = {"ios", "macos"}
# Mirrors swarmqa.models.FindingKind and category_for_kind. Kept here so the
# script runs without the swarmqa package installed.
KINDS = {
    "crash", "timeout", "assertion", "missing_control", "visual",
    "visual_judgment", "suite_failure", "unresponsive", "error_state",
    "launch", "friction_path",
}
CATEGORY_BY_KIND = {
    "crash": "crash",
    "launch": "crash",
    "visual": "visual",
    "visual_judgment": "visual",
    "friction_path": "confusing",
}
REQUIRED = {
    "id": str, "identifier": str, "screen": str, "path": list,
    "category": str, "kind": str, "platforms": list, "description": str,
}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z][A-Za-z0-9]*(\.[A-Za-z0-9]+)+$")


def source_identifiers() -> tuple[set[str], list[str], set[str]]:
    """Literal identifiers and interpolated prefixes used in the sources.

    `.accessibilityIdentifier("home.row.\\(task.id)")` yields the prefix
    `home.row.`; a concrete `home.row.report` then matches when `report`
    is a seeded model id in the sources (`id: "report"`).
    Returns (literal identifiers, interpolated prefixes, seeded ids).
    """
    literals: set[str] = set()
    prefixes: list[str] = []
    strings: set[str] = set()  # seeded ids
    for swift in sorted(SOURCES.rglob("*.swift")):
        text = swift.read_text(encoding="utf-8")
        strings.update(re.findall(r'\bid:\s*"([^"\\]+)"', text))
        for arg in re.findall(r'accessibilityIdentifier\(\s*"((?:[^"\\]|\\.)*)"\s*\)', text):
            if "\\(" in arg:
                prefixes.append(arg.split("\\(", 1)[0])
            else:
                literals.add(arg)
    return literals, prefixes, strings


def identifier_exists(
    identifier: str, literals: set[str], prefixes: list[str], strings: set[str]
) -> bool:
    if identifier in literals:
        return True
    for prefix in prefixes:
        if identifier.startswith(prefix) and identifier[len(prefix):] in strings:
            return True
    return False


def check(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read {path}: {exc}"]

    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if data.get("app") != "PlantedBugs":
        errors.append("app must be 'PlantedBugs'")
    if data.get("bundle_id") != "dev.swarmqa.PlantedBugs":
        errors.append("bundle_id must be 'dev.swarmqa.PlantedBugs'")
    bugs = data.get("bugs")
    if not isinstance(bugs, list) or not bugs:
        return errors + ["bugs must be a non-empty list"]

    literals, prefixes, strings = source_identifiers()
    if not literals:
        errors.append(f"no accessibility identifiers found under {SOURCES}")

    seen_ids: set[str] = set()
    for index, bug in enumerate(bugs):
        where = f"bugs[{index}]"
        if not isinstance(bug, dict):
            errors.append(f"{where}: must be an object")
            continue
        where = f"bugs[{index}] ({bug.get('id', '?')})"
        for field, kind in REQUIRED.items():
            if not isinstance(bug.get(field), kind):
                errors.append(f"{where}: '{field}' must be a {kind.__name__}")
        extra = set(bug) - set(REQUIRED)
        if extra:
            errors.append(f"{where}: unknown fields {sorted(extra)}")
        if any(not isinstance(bug.get(f), t) for f, t in REQUIRED.items()):
            continue

        if not ID_PATTERN.match(bug["id"]):
            errors.append(f"{where}: id must be lowercase kebab-case")
        if bug["id"] in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(bug["id"])
        if not bug["screen"].strip() or not bug["description"].strip():
            errors.append(f"{where}: screen and description must not be empty")

        if bug["category"] not in CATEGORIES:
            errors.append(f"{where}: category must be one of {sorted(CATEGORIES)}")
        if bug["kind"] not in KINDS:
            errors.append(f"{where}: kind '{bug['kind']}' is not a Finding kind")
        elif CATEGORY_BY_KIND.get(bug["kind"], "broken") != bug["category"]:
            errors.append(
                f"{where}: kind '{bug['kind']}' maps to category "
                f"'{CATEGORY_BY_KIND.get(bug['kind'], 'broken')}', not '{bug['category']}'"
            )

        platforms = bug["platforms"]
        if not platforms or not set(platforms) <= PLATFORMS or len(set(platforms)) != len(platforms):
            errors.append(f"{where}: platforms must be a non-empty subset of {sorted(PLATFORMS)}")

        for step in [bug["identifier"], *bug["path"]]:
            if not isinstance(step, str) or not IDENTIFIER_PATTERN.match(step):
                errors.append(f"{where}: '{step}' is not a dotted accessibility identifier")
            elif not identifier_exists(step, literals, prefixes, strings):
                errors.append(f"{where}: identifier '{step}' not found in {SOURCES.name}/*.swift")

    identifiers = [b.get("identifier") for b in bugs if isinstance(b, dict)]
    if len(set(identifiers)) != len(identifiers):
        errors.append("each bug must have its own identifier")
    return errors


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else HERE / "bugs.json"
    errors = check(path)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    count = len(json.loads(path.read_text(encoding="utf-8"))["bugs"])
    print(f"ok: {count} planted bugs, all identifiers found in the sources")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
