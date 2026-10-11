"""WP-D1: turn a code change into prioritised intents.

`collect_change` reads the diff between two refs (or the working tree) and
the changed UI files, filtered by `[flows] include` / `exclude` and cut to
size. `propose` hands them to the ModelProvider's `propose_flows`, and
`write_intents` writes one markdown intent per flow in the format
`intent/ingest.py` already reads. File names start with the rank, so a
directory of them loads most-at-risk first.

Every git call runs with `git -C <repo>`, so `repo` may be a working copy or
the builder's bare mirror (`~/.aqa/repos/<name>.git`).
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from swarmqa.errors import AQAError
from swarmqa.flows.settings import FlowsSettings
from swarmqa.llm.protocol import ChangedFile, ModelProvider, ProposedFlow
from swarmqa.util import slug

GENERATED_TAG = "from-diff"
_TRUNCATED = "\n... [cut: over the size limit]\n"

# (args, cwd) -> (returncode, stdout, stderr)
Git = Callable[[list[str], Path], tuple[int, str, str]]


class FlowsError(AQAError):
    """The change could not be read."""


@dataclass
class Change:
    base: str
    head: str | None
    diff: str = ""
    files: list[ChangedFile] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    truncated: bool = False


def _git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _matches(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _cut(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore") + _TRUNCATED, True


def collect_change(
    repo: str | Path,
    base: str,
    head: str | None = "HEAD",
    *,
    settings: FlowsSettings | None = None,
    git: Git = _git,
) -> Change:
    """The UI-relevant part of `base...head`.

    `head=None` compares `base` with the working tree (uncommitted changes
    included). Deleted files are listed in the diff but have no content.
    """
    settings = settings or FlowsSettings()
    repo = Path(repo).expanduser()
    span = [base] if head is None else [f"{base}...{head}"]
    code, out, err = git(["diff", "--name-only", "--no-renames", *span], repo)
    if code != 0:
        raise FlowsError(f"git diff {' '.join(span)} failed in {repo}: {err.strip() or code}")
    changed = [line.strip() for line in out.splitlines() if line.strip()]
    relevant = [path for path in changed if _matches(path, settings.include) and not _matches(path, settings.exclude)]
    change = Change(base=base, head=head, changed=changed)
    if not relevant:
        return change
    code, diff, err = git(["diff", "--unified=3", "--no-renames", *span, "--", *relevant], repo)
    if code != 0:
        raise FlowsError(f"git diff failed in {repo}: {err.strip() or code}")
    change.diff, change.truncated = _cut(diff, settings.max_diff_bytes)
    for path in relevant[: settings.max_files]:
        if head is None:
            local = repo / path
            if not local.is_file():
                continue
            content = local.read_text(encoding="utf-8", errors="replace")
        else:
            code, content, _ = git(["show", f"{head}:{path}"], repo)
            if code != 0:
                continue  # deleted at head
        content, cut = _cut(content, settings.max_file_bytes)
        change.truncated = change.truncated or cut
        change.files.append(ChangedFile(path=path, content=content))
    change.truncated = change.truncated or len(relevant) > settings.max_files
    return change


def propose(
    provider: ModelProvider,
    change: Change,
    *,
    max_flows: int = 10,
    timeout_s: float = 120.0,
) -> list[ProposedFlow]:
    """Flows the change puts at risk, most important first. No call when nothing UI-relevant changed."""
    if not change.diff.strip():
        return []
    proposal = provider.propose_flows(change.diff, list(change.files), max_flows=max_flows, timeout_s=timeout_s)
    flows: list[ProposedFlow] = []
    names: set[str] = set()
    for flow in proposal.flows:
        name = (flow.name or "").strip()
        goal = (flow.goal or "").strip()
        if not name or not goal or name.lower() in names:
            continue
        names.add(name.lower())
        flow.priority = min(5, max(1, int(flow.priority or 3)))
        flows.append(flow)
    flows.sort(key=lambda flow: flow.priority)
    return flows[:max_flows]


def render_intent(flow: ProposedFlow) -> str:
    """One markdown intent. Hints and expectations go into the goal, not `## Steps`
    (that section is the strict scripted grammar)."""
    lines = ["---", f"tags: [{GENERATED_TAG}, priority:{flow.priority}]", "---", "", f"# {flow.name.strip()}", ""]
    lines += [flow.goal.strip(), ""]
    for heading, items in (
        ("How to get there", flow.steps),
        ("Expected", flow.expected),
        ("Changed files", flow.touched_files),
    ):
        items = [str(item).strip() for item in items if str(item).strip()]
        if items:
            lines += [f"## {heading}", "", *[f"- {item}" for item in items], ""]
    return "\n".join(lines)


def write_intents(flows: list[ProposedFlow], out_dir: str | Path) -> list[Path]:
    """Write `<rank>-p<priority>-<slug>.md` files, replacing earlier generated ones."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.md"):
        if f"tags: [{GENERATED_TAG}," in old.read_text(encoding="utf-8", errors="replace")[:200]:
            old.unlink()
    written: list[Path] = []
    for rank, flow in enumerate(flows, start=1):
        path = out / f"{rank:02d}-p{flow.priority}-{slug(flow.name)}.md"
        path.write_text(render_intent(flow), encoding="utf-8")
        written.append(path)
    return written


def provider_for(config) -> ModelProvider:
    """The `[llm]` provider. Raises FlowsError when `[llm]` is off: proposing flows needs a model."""
    if not config.llm.enabled:
        raise FlowsError("flows from the diff need a model: set [llm] enabled = true")
    from swarmqa.config import llm_settings
    from swarmqa.llm.settings import create_provider

    return create_provider(llm_settings(config))


def intents_from_diff(
    config,
    repo: str | Path,
    base: str,
    head: str | None,
    out_dir: str | Path,
    *,
    provider: ModelProvider | None = None,
    git: Git = _git,
    log: Callable[[str], None] = print,
) -> list[Path]:
    """Collect the change, propose flows and write them as intents under `out_dir`."""
    settings = FlowsSettings.from_config(config)
    change = collect_change(repo, base, head, settings=settings, git=git)
    span = f"{base}...{head}" if head else f"{base}..working tree"
    if not change.diff.strip():
        log(f"flows: no UI files changed in {span} ({len(change.changed)} file(s) changed)")
        return []
    provider = provider or provider_for(config)
    flows = propose(provider, change, max_flows=settings.max_flows)
    paths = write_intents(flows, out_dir)
    cut = " (diff cut to size)" if change.truncated else ""
    log(f"flows: {len(paths)} intent(s) from {len(change.files)} changed UI file(s) in {span}{cut} -> {out_dir}")
    return paths
