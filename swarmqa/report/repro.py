"""Repro scripts per finding, and replaying one to see if the bug is still there.

Every finding gets `findings/<id>.replay.json` (a version-1 flow; the agent
loop and scripted explorer already write it) and `findings/<id>.repro.md`
with the command that replays it. `Finding.repro` is the replay path,
relative to the campaign directory.

`replay_finding` runs the flow with `run_scripted` through a driver wrapper
that runs the checks around every interaction. The reproduction rule:

- `crash`: the final step crashed the app, or a crash with the finding's
  fingerprint was seen at any step.
- `launch`: the app failed to launch again.
- anything else: the finding's fingerprint was seen by a check at any step,
  or the final step failed.

Steps keep going after a failure (`on_step_failure = "continue"`), as the
agent loop did in the recorded session. See docs/evidence.md.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from swarmqa.checks.protocol import Check, CheckIssue, StepContext
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.driver.v1_adapter import as_v2
from swarmqa.errors import AppCrashedError, UITimeoutError
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import CampaignConfig, Finding, Shard, WorkerResult
from swarmqa.report.findings_json import FINDINGS_JSON, load_findings_json
from swarmqa.report.paths import public, repro_command, resolve
from swarmqa.reporter.findings import fingerprint_for, write_replay

# Repro files ----------------------------------------------------------------


def replay_path_for(campaign_dir: Path, finding: Finding) -> Path:
    """Where the finding's replay flow is (or would be written)."""
    if finding.replay_json:
        return resolve(campaign_dir, finding.replay_json)
    return Path(campaign_dir) / "findings" / f"{finding.id}.replay.json"


def reproduction_rule(kind: str) -> str:
    """One sentence saying when a replay counts as reproducing a finding of `kind`."""
    if kind == "crash":
        return "The final step crashes the app, or a crash with the same fingerprint is seen."
    if kind == "launch":
        return "The app fails to launch."
    return "A check reports the same fingerprint at any step, or the final step fails."


def ensure_repro(campaign_dir: Path, finding: Finding) -> Path | None:
    """Make sure the replay flow exists, set `repro`, and write `<id>.repro.md`.

    An existing replay is reused. A launch finding without one gets a
    one-step `launch` flow; any other finding without one gets no repro.
    Returns the replay path, or None.
    """
    campaign_dir = Path(campaign_dir)
    path = replay_path_for(campaign_dir, finding)
    if not path.is_file():
        if finding.kind != "launch":
            return None
        path = write_replay(finding.id, campaign_dir, [{"action": "launch"}])
    relative = public(campaign_dir, path)
    finding.replay_json = finding.replay_json or relative
    finding.repro = relative
    write_repro_md(campaign_dir, finding, path)
    return path


def write_repro_md(campaign_dir: Path, finding: Finding, replay_path: Path) -> Path:
    destination = Path(campaign_dir) / "findings" / f"{finding.id}.repro.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        steps = json.loads(Path(replay_path).read_text(encoding="utf-8")).get("steps", [])
    except (OSError, ValueError):
        steps = []
    lines = [
        f"# Reproduce: {finding.title}",
        "",
        f"Finding `{finding.id}` ({finding.kind}, fingerprint `{finding.fingerprint}`).",
        "",
        "```sh",
        repro_command(replay_path),
        "```",
        "",
        f"Reproduced when: {reproduction_rule(finding.kind)}",
        "",
        f"## Replay steps ({len(steps)})",
        "",
    ]
    lines.extend(f"{index + 1}. `{json.dumps(step, sort_keys=True)}`" for index, step in enumerate(steps))
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


# Replay ---------------------------------------------------------------------


@dataclass
class ReplayResult:
    """What replaying a finding showed. `reason` says which rule matched, or why none did."""

    reproduced: bool
    reason: str
    finding_id: str = ""
    kind: str = ""
    fingerprint: str = ""
    observed_fingerprints: list[str] = field(default_factory=list)
    final_step_failed: bool = False
    crashed: bool = False
    result: WorkerResult | None = None
    # False when the app never launched, so the flow could not be checked
    # (except for a launch finding, where that is the reproduction).
    ran: bool = True


def replay_finding(
    replay_path: Path,
    config: CampaignConfig,
    *,
    driver=None,
    work_dir: Path,
    finding: Finding | None = None,
    checks: Iterable[Check] | None = None,
    driver_factory: Callable[[Any, Path], Any] | None = None,
) -> ReplayResult:
    """Replay a finding's flow and report whether the finding reproduced.

    The finding is looked up by id in `<campaign>/findings.json` next to the
    replay's `findings/` directory unless `finding` is given. Without one,
    any failed step counts. `driver` defaults to the one the config selects,
    created in `work_dir`. `checks` default to `default_checks()` (functional
    and layout). Artifacts from the replay go under `work_dir`.
    """
    from swarmqa.checks import default_checks
    from swarmqa.explorer.scripted import run_scripted
    from swarmqa.intent.ingest import action_from_dict

    replay_path = Path(replay_path)
    document = json.loads(replay_path.read_text(encoding="utf-8"))
    finding_id = str(document.get("name") or replay_path.name.removesuffix(".replay.json"))
    if finding is None:
        finding = _lookup_finding(replay_path, finding_id)
    actions = [action_from_dict(step, origin=str(replay_path)) for step in document.get("steps", [])]
    # run_scripted launches the app itself.
    if actions and actions[0].action == "launch":
        actions = actions[1:]

    run_dir = Path(work_dir) / f"replay-{finding_id or 'flow'}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if driver is None:
        driver = (driver_factory or _default_driver_factory(config))(config.app, run_dir)
    watcher = _WatchingDriver(driver, default_checks() if checks is None else checks)
    replay_config = copy.deepcopy(config)
    replay_config.explorer.on_step_failure = "continue"
    shard = Shard(id="replay", kind="scripted", name=f"replay {finding_id}", actions=actions)
    result = run_scripted(shard, watcher, replay_config, worker_id="replay", work_dir=run_dir)
    return _judge(result, watcher, finding, finding_id, len(actions))


def _judge(
    result: WorkerResult, watcher: "_WatchingDriver", finding: Finding | None, finding_id: str, count: int
) -> ReplayResult:
    observed = list(dict.fromkeys(watcher.fingerprints))
    last = count - 1
    final_failed = any(step.index == last and step.status == "failed" for step in result.steps) if count else False
    launch_failed = any(item.kind == "launch" for item in result.findings)
    # A scripted crash finding lists every step run up to and including the crash.
    # Only the first crash counts: later steps on a dead app crash too.
    crash_lengths = [len(item.steps) for item in result.findings if item.kind == "crash"]
    final_crash = count > 0 and bool(crash_lengths) and min(crash_lengths) == count
    crashed = bool(watcher.crash_calls) or any(item.kind == "crash" for item in result.findings)
    base = ReplayResult(
        reproduced=False,
        reason="",
        finding_id=finding_id,
        kind=finding.kind if finding else "",
        fingerprint=finding.fingerprint if finding else "",
        observed_fingerprints=observed,
        final_step_failed=final_failed,
        crashed=crashed,
        result=result,
        ran=not launch_failed or (finding is not None and finding.kind == "launch"),
    )
    if finding is None:
        failed = launch_failed or any(step.status == "failed" for step in result.steps)
        base.reproduced = failed
        base.reason = "a step failed (no finding metadata)" if failed else "every step passed (no finding metadata)"
        return base
    same = finding.fingerprint in observed
    if finding.kind == "launch":
        base.reproduced = launch_failed
        base.reason = "the app failed to launch" if launch_failed else "the app launched"
    elif finding.kind == "crash":
        if final_crash:
            base.reproduced, base.reason = True, "the final step crashed the app"
        elif same:
            base.reproduced, base.reason = True, "a crash with the same fingerprint was seen"
        else:
            base.reason = "the final step did not crash the app"
    elif same:
        base.reproduced, base.reason = True, "a check reported the same fingerprint"
    elif final_failed:
        base.reproduced, base.reason = True, "the final step failed"
    elif launch_failed:
        base.reason = "the app failed to launch before the flow ran"
    else:
        base.reason = "the flow ran and the finding was not seen"
    return base


def _lookup_finding(replay_path: Path, finding_id: str) -> Finding | None:
    path = replay_path.resolve().parent.parent / FINDINGS_JSON
    if not path.is_file():
        return None
    try:
        findings = load_findings_json(path)
    except (OSError, ValueError, TypeError):
        return None
    for item in findings:
        if item.id == finding_id:
            return item
    return None


def _default_driver_factory(config: CampaignConfig) -> Callable[[Any, Path], Any]:
    def factory(target, work_dir: Path):
        from swarmqa.driver import create_driver

        kind = None if config.driver.kind == "auto" else config.driver.kind
        return create_driver(target, work_dir, kind=kind, video_mode=config.video.mode)

    return factory


class _WatchingDriver:
    """Wraps a driver and runs the checks around every interaction.

    Everything else passes through, so `run_scripted` drives it like the
    real driver. Fingerprints use `CheckIssue.to_finding`'s formula.
    """

    def __init__(self, driver, checks: Iterable[Check]):
        self._inner = as_v2(driver)
        self._checks = list(checks)
        self.fingerprints: list[str] = []
        self.issues: list[CheckIssue] = []
        self.calls = 0
        self.crash_calls: list[int] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def click(self, target) -> None:
        self._watch(StepDecision(kind="tap", target=target), lambda: self._inner.click(target))

    def type_text(self, target, text: str) -> None:
        self._watch(StepDecision(kind="type", target=target, text=text), lambda: self._inner.type_text(target, text))

    def tap_point(self, x: float, y: float) -> None:
        self._watch(StepDecision(kind="tap_point", point=(x, y)), lambda: self._inner.tap_point(x, y))

    def swipe(self, start, end, duration_s: float = 0.3) -> None:
        decision = StepDecision(kind="swipe", point=tuple(start), end=tuple(end))
        self._watch(decision, lambda: self._inner.swipe(start, end, duration_s))

    def keychord(self, keys: list[str]) -> None:
        self._watch(StepDecision(kind="key", keys=list(keys)), lambda: self._inner.keychord(keys))

    def _observe(self) -> ScreenObservation | None:
        try:
            return self._inner.observe(screenshot=True)
        except Exception:  # noqa: BLE001 - no observation means no checks for this step
            return None

    def _watch(self, decision: StepDecision, act: Callable[[], None]) -> None:
        index = self.calls
        self.calls += 1
        before = self._observe()
        since = time.time()
        try:
            act()
        except AppCrashedError as exc:
            self.crash_calls.append(index)
            after = ScreenObservation(tree=[], ts=time.time())
            self._run(StepContext(after, self._inner, before, decision, since, "", index, "", True, str(exc)))
            raise
        except UITimeoutError as exc:
            after = self._observe() or ScreenObservation(tree=[], ts=time.time())
            self._run(StepContext(after, self._inner, before, decision, since, "", index, "", False, str(exc)))
            raise
        after = self._observe()
        if before is None or after is None:
            return
        self._run(StepContext(after, self._inner, before, decision, since, "", index))

    def _run(self, ctx: StepContext) -> None:
        for check in self._checks:
            try:
                found = check.run(ctx)
            except Exception:  # noqa: BLE001 - a broken check never stops the replay
                continue
            for issue in found:
                self.issues.append(issue)
                self.fingerprints.append(fingerprint_for(issue.kind, issue.title, issue.target()))
