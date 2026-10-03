"""WP-D1: goal shards from a code change.

`collect_change` reads the diff and the touched files from git;
`flows_from_diff` asks the model which flows the change puts at risk and
turns them into exploratory goal shards, most important first. Without a
model, or when it fails, heuristics over the changed Swift views stand in.
See docs/flows.md.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from swarmqa.devices.commands import Runner, run
from swarmqa.errors import AQAError
from swarmqa.flows import FLOW_TAG, Change, FlowPlan
from swarmqa.flows.settings import FlowsSettings
from swarmqa.llm.protocol import ChangedFile, ModelError, ModelProvider, ProposedFlow
from swarmqa.models import Shard

MIN_PRIORITY = 1
MAX_PRIORITY = 5
MAX_HEURISTIC_CONTROLS = 6

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_IDENTIFIER = re.compile(r'\.accessibilityIdentifier\(\s*"([^"\\]+)"')
_CONTROL = re.compile(r'\b(?:Button|NavigationLink|Toggle)\s*\(\s*(?:LocalizedStringKey\(\s*)?"([^"\\]+)"')
_TYPE_DECL = re.compile(
    r"^\s*(?:(?:public|private|fileprivate|internal|final|@\w+(?:\([^)]*\))?)\s+)*"
    r"(struct|class|enum|actor|extension)\s+([A-Za-z_]\w*)(?:\s*<[^>{]*>)?\s*(?::\s*([^{]+))?"
)
_SINGLE_QUOTED = re.compile(r"(?<![A-Za-z0-9])'([^'\n]+)'(?![A-Za-z0-9])")
_DOUBLE_QUOTED = re.compile(r'"([^"]+)"')


class ChangeError(AQAError):
    """git could not produce the diff for a change."""


def git_runner(args: list[str], *, timeout: float | None = None, **_: Any) -> Any:
    """Run git with UTF-8 output; undecodable bytes are replaced, not raised."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )


def collect_change(
    repo_dir: str | Path,
    base: str,
    head: str,
    *,
    max_chars: int | None = None,
    settings: FlowsSettings | None = None,
    runner: Runner = git_runner,
) -> Change:
    """Diff `base...head` (or `base..head` without a merge base) and read the touched files at `head`.

    The diff is cut to `max_chars` (default `max_diff_chars`). Touched files
    matching `file_globs` are read at `head`, each cut to `max_file_chars`;
    deleted ones are listed with empty content. Raise ChangeError when git fails.
    """
    settings = settings or FlowsSettings()
    limit = max_chars if max_chars is not None else settings.max_diff_chars
    git = ["git", "-C", str(repo_dir), "-c", "core.quotepath=off"]

    spec = f"{base}...{head}"
    diff = run(runner, [*git, "diff", "--no-color", "--no-ext-diff", "--no-renames", spec])
    if not diff.ok:
        spec = f"{base}..{head}"
        diff = run(runner, [*git, "diff", "--no-color", "--no-ext-diff", "--no-renames", spec])
    if not diff.ok:
        raise ChangeError(f"git diff {base} {head} failed: {diff.detail()}")
    names = run(runner, [*git, "diff", "--name-status", "-z", "--no-renames", spec])
    if not names.ok:
        raise ChangeError(f"git diff --name-status {spec} failed: {names.detail()}")

    text = diff.stdout
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]

    files: list[ChangedFile] = []
    for status, path in _name_status(names.stdout):
        if not _matches(path, settings.file_globs):
            continue
        if status.startswith("D"):
            files.append(ChangedFile(path=path, content=""))
            continue
        shown = run(runner, [*git, "show", f"{head}:{path}"])
        content = shown.stdout if shown.ok else ""
        files.append(ChangedFile(path=path, content=content[: settings.max_file_chars]))
    return Change(base=base, head=head, diff=text, files=files, truncated=truncated)


def flows_from_diff(
    config: Any,
    change: Change,
    *,
    provider: ModelProvider | None = None,
    max_flows: int | None = None,
    settings: FlowsSettings | None = None,
) -> FlowPlan:
    """Goal shards for `change`, most important first, capped at `max_flows`.

    Uses `provider.propose_flows` when given; with no provider, or on
    ModelError, falls back to heuristics over the changed Swift views.
    """
    settings = settings or FlowsSettings.from_config(config)
    cap = max_flows if max_flows is not None else settings.max_flows
    if not settings.from_diff:
        return FlowPlan(source="none", notes=["flows.from_diff is off"])
    if not change.diff.strip():
        return FlowPlan(source="none", notes=["empty diff"])

    notes: list[str] = []
    if change.truncated:
        notes.append(f"diff cut to {len(change.diff)} characters")
    if provider is not None:
        try:
            proposal = provider.propose_flows(change.diff, list(change.files), max_flows=cap)
        except ModelError as exc:
            notes.append(f"model failed ({type(exc).__name__}: {exc}); used heuristics")
        else:
            shards = _shards(proposal.flows, cap)
            if not shards:
                notes.append("model proposed no flows")
            return FlowPlan(shards=shards, source="model", notes=notes, usage=proposal.usage)
    else:
        notes.append("no model provider; used heuristics")

    flows = heuristic_flows(change)
    if not flows:
        notes.append("no SwiftUI views in the change")
    return FlowPlan(shards=_shards(flows, cap), source="heuristic", notes=notes)


def heuristic_flows(change: Change) -> list[ProposedFlow]:
    """One flow per touched SwiftUI view: open it and use the controls the change added."""
    contents = {item.path: item.content for item in change.files}
    touched: dict[str, _TouchedView] = {}
    for patch in _patches(change.diff):
        if not patch.path.endswith(".swift"):
            continue
        content = contents.get(patch.path, "")
        known = dict(enumerate(content.splitlines(), start=1))
        known.update(patch.lines)
        decls = _declarations(sorted(known.items()))
        views = {name for kind, name, _ in decls if kind == "view"}
        fallback = "" if content else next((name for kind, name, _ in decls if kind == "view"), "")

        events = [(line_no, text) for line_no, text in patch.added] + [(line_no, "") for line_no in patch.removed]
        for line_no, text in sorted(events, key=lambda event: event[0]):
            view = _enclosing(decls, line_no, views) or fallback
            controls = _controls(text)
            if not view and controls:
                view = PurePosixPath(patch.path).stem
            if not view:
                continue
            entry = touched.setdefault(view, _TouchedView(view, patch.path))
            for control in controls:
                if control not in entry.controls:
                    entry.controls.append(control)

    flows: list[ProposedFlow] = []
    for entry in touched.values():
        title = _humanize(entry.view)
        controls = entry.controls[:MAX_HEURISTIC_CONTROLS]
        goal = f"Open {title}"
        if controls:
            goal += " and use " + _join([f'"{control}"' for control in controls])
        flows.append(
            ProposedFlow(
                name=f"{title} view",
                goal=goal,
                priority=2 if controls else 3,
                touched_files=[entry.path],
            )
        )
    return flows


def goal_text(goal: str, steps: list[str], expected: list[str]) -> str:
    """Join a goal, its step hints and expected outcomes into one agent goal.

    `parse_goal` treats double-quoted phrases as the controls that mark the
    goal done, so quotes are kept in the goal and the expected outcomes and
    dropped from the step hints, except the last quoted step when nothing
    else is quoted. Single quotes become double quotes; apostrophes become `’`.
    """
    goal = _sentence(_normalize_quotes(goal))
    steps = [_normalize_quotes(item).strip().rstrip(".;") for item in steps if item and item.strip()]
    expected = [_normalize_quotes(item).strip().rstrip(".;") for item in expected if item and item.strip()]
    keep = -1
    if not _DOUBLE_QUOTED.search(" ".join([goal, *expected])):
        keep = next((i for i in range(len(steps) - 1, -1, -1) if _DOUBLE_QUOTED.search(steps[i])), -1)
    steps = [item if i == keep else _DOUBLE_QUOTED.sub(r"\1", item) for i, item in enumerate(steps)]
    parts = [goal] if goal else []
    if steps:
        parts.append("Steps: " + "; ".join(steps) + ".")
    if expected:
        parts.append("Expect: " + "; ".join(expected) + ".")
    return " ".join(parts)


def flow_shard(flow: ProposedFlow) -> Shard:
    """An exploratory goal shard for one proposed flow. The id is left for `build_queue`."""
    from swarmqa.intent.ingest import _intent_tags

    name = " ".join((flow.name or flow.goal).split())
    priority = _priority(flow.priority)
    return Shard(
        id="",
        kind="exploratory",
        name=name,
        goal=goal_text(flow.goal or flow.name, list(flow.steps), list(flow.expected)),
        tags=_intent_tags(name, [FLOW_TAG, f"priority:{priority}"]),
    )


@dataclass
class _Patch:
    path: str
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    lines: dict[int, str] = field(default_factory=dict)


@dataclass
class _TouchedView:
    view: str
    path: str
    controls: list[str] = field(default_factory=list)


def _shards(flows: list[ProposedFlow], cap: int) -> list[Shard]:
    usable = [(i, flow) for i, flow in enumerate(flows) if (flow.name or "").strip() or (flow.goal or "").strip()]
    usable.sort(key=lambda pair: (_priority(pair[1].priority), pair[0]))
    seen: set[str] = set()
    shards: list[Shard] = []
    for _, flow in usable:
        keys = {_norm(flow.name), _norm(flow.goal)} - {""}
        if keys & seen:
            continue
        seen |= keys
        shards.append(flow_shard(flow))
        if len(shards) >= cap:
            break
    return shards


def _priority(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 3
    return min(MAX_PRIORITY, max(MIN_PRIORITY, number))


def _norm(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _name_status(output: str) -> list[tuple[str, str]]:
    parts = output.split("\0")
    pairs: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(parts):
        status, path = parts[index].strip(), parts[index + 1]
        index += 2
        if status and path:
            pairs.append((status, path))
    return pairs


def _matches(path: str, globs: list[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob) for glob in globs)


def _patches(diff: str) -> list[_Patch]:
    patches: list[_Patch] = []
    current: _Patch | None = None
    line_no = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current, in_hunk = None, False
            continue
        if not in_hunk and line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = _Patch(path=target[2:] if target.startswith("b/") else target)
                patches.append(current)
            continue
        hunk = _HUNK.match(line)
        if hunk:
            line_no, in_hunk = int(hunk.group(1)), True
            continue
        if current is None or not in_hunk:
            continue
        if line.startswith("+"):
            current.added.append((line_no, line[1:]))
            current.lines[line_no] = line[1:]
            line_no += 1
        elif line.startswith("-"):
            current.removed.append(line_no)
        elif line.startswith(" ") or not line:
            current.lines[line_no] = line[1:]
            line_no += 1
    return patches


def _declarations(lines: list[tuple[int, str]]) -> list[tuple[str, str, int]]:
    """Type declarations in numbered lines as (kind, name, line); kind is `view`, `extension` or `other`."""
    found: list[tuple[str, str, int]] = []
    for line_no, line in lines:
        match = _TYPE_DECL.match(line)
        if not match:
            continue
        keyword, name, conformances = match.group(1), match.group(2), match.group(3) or ""
        protocols = {part.strip().split("<")[0] for part in conformances.split(",")}
        if keyword == "extension":
            kind = "extension"
        elif keyword == "struct" and protocols & {"View", "SwiftUI.View"}:
            kind = "view"
        else:
            kind = "other"
        found.append((kind, name, line_no))
    return found


def _enclosing(decls: list[tuple[str, str, int]], line_no: int, views: set[str]) -> str:
    """The view whose declaration (or extension) last precedes `line_no`, or ''."""
    owner = ""
    for kind, name, decl_line in decls:
        if decl_line > line_no:
            break
        owner = name if kind == "view" or (kind == "extension" and name in views) else ""
    return owner


def _controls(text: str) -> list[str]:
    out: list[str] = []
    for pattern in (_IDENTIFIER, _CONTROL):
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if value and value not in out:
                out.append(value)
    return out


def _humanize(name: str) -> str:
    base = name[:-4] if name.endswith("View") and len(name) > 4 else name
    return " ".join(re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", base)) or name


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _normalize_quotes(text: str) -> str:
    text = (text or "").replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    text = _SINGLE_QUOTED.sub(r'"\1"', text)
    text = text.replace("'", "’")
    if text.count('"') % 2:
        text = text.replace('"', "")
    return " ".join(text.split())


def _sentence(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text
