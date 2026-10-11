"""Markdown and JSON campaign summaries.

Orchestrator and the empty-campaign path both call these writers so the
on-disk schema stays one shape.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.models import CampaignResult, Finding
from swarmqa.report.paths import repro_command, resolve
from swarmqa.serialize import dump_json, to_plain

# Pixel diffs and appearance judgments share the main findings list.
_VISUAL_KINDS = frozenset({"visual", "visual_judgment"})
SEVERITIES = ("critical", "high", "medium", "low")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
_CONFIRM_RANK = {"confirmed": 0, "flaky": 1, None: 2, "skipped": 3, "not_reproduced": 4}
_CONFIRM_LABELS = {"confirmed": "Confirmed", "flaky": "Flaky", "not_reproduced": "Not reproduced",
                   "skipped": "Not re-run"}
TO_CHECK_NOTE = "advisory or not reproduced"


def finding_sort_key(finding: Finding) -> tuple:
    """Severity first, then confirmed before unconfirmed, then higher confidence."""
    return (
        _SEVERITY_RANK.get(finding.severity, 9),
        _CONFIRM_RANK.get(finding.confirmation, 2),
        -float(finding.confidence),
        finding.id,
    )


def is_problem(finding: Finding) -> bool:
    """A finding to act on: not advisory and not shown to be unreproducible."""
    return not finding.advisory and finding.confirmation != "not_reproduced"


def confirmation_text(finding: Finding) -> str | None:
    """A label like "Confirmed 2/2", "Flaky 1/2" or "Not reproduced 0/2"; None when not checked."""
    if not finding.confirmation:
        return None
    label = _CONFIRM_LABELS.get(finding.confirmation, finding.confirmation.replace("_", " ").capitalize())
    if finding.confirm_attempts and finding.confirmation != "skipped":
        label += f" {finding.confirm_hits}/{finding.confirm_attempts}"
    return label


def occurrence_count(finding: Finding) -> int:
    """How many places showed this problem: the finding itself plus its occurrences."""
    return 1 + len(finding.occurrences)


def verdict(findings: list[Finding]) -> str:
    """The result in plain words, e.g. "3 problems found, 1 critical" or "No problems found"."""
    problems = [finding for finding in findings if is_problem(finding)]
    rest = len(findings) - len(problems)
    if problems:
        counts = [f"{n} {name}" for name in SEVERITIES if (n := sum(f.severity == name for f in problems))]
        text = ", ".join([f"{_plural(len(problems), 'problem')} found", *counts])
    else:
        text = "No problems found"
    if rest:
        noun = "more finding" if problems else "finding"
        text += f"; {_plural(rest, noun)} to check ({TO_CHECK_NOTE})"
    return text


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def write_summary(result: CampaignResult, root: Path | None = None) -> Path:
    directory = root or Path(result.report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "summary.json"
    md_path = directory / "summary.md"
    dump_json(result, json_path)
    md_path.write_text(render_summary_md(result), encoding="utf-8")
    return md_path


def render_summary_md(result: CampaignResult) -> str:
    coverage = result.coverage
    spend = result.spend
    every = [finding for item in result.results for finding in item.findings]
    lines = [
        f"# Campaign {result.campaign_id}",
        "",
        f"**{verdict(every)}.**",
        "",
        f"Backend: `{result.backend}`",
        "",
        "## Coverage",
        "",
        f"- completed: {coverage.completed}",
        f"- failed: {coverage.failed}",
        f"- cancelled: {coverage.cancelled}",
        f"- not_started: {coverage.not_started}",
        f"- stop_reason: {coverage.stop_reason or 'none'}",
        "",
        "## Spend",
        "",
        f"- max_spend: {_fmt_optional(spend.max_spend)}",
        f"- currency: {spend.currency}",
        f"- estimated_spent: {spend.estimated_spent:.4f}",
        f"- stop_reason: {spend.stop_reason or 'none'}",
        f"- note: {spend.note or ''}",
        "",
        "## Scripted matrix",
        "",
        "| Shard | Kind | Result | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for item in result.results:
        evidence = _evidence(item.findings)
        lines.append(
            f"| {item.shard_name or item.shard_id} | {item.shard_kind or ''} | {item.status} | {evidence} |"
        )
    report_dir = Path(result.report_dir) if result.report_dir else None
    findings = sorted(
        (finding for finding in every if finding.kind != "friction_path"), key=finding_sort_key
    )
    firm = [finding for finding in findings if is_problem(finding)]
    advisory = [finding for finding in findings if not is_problem(finding)]
    lines.extend(["", "## Findings", ""])
    if not firm:
        lines.append("No findings.")
    for finding in firm:
        lines.append(_bullet(finding))
        lines.extend(_detail_lines(finding, report_dir))
    lines.extend(["", "## Advisory findings", ""])
    lines.append(
        "Reported only by a model or a soft heuristic, or not seen again on a re-run; check before acting."
    )
    lines.append("")
    if not advisory:
        lines.append("No advisory findings.")
    for finding in advisory:
        lines.append(_bullet(finding))
        lines.extend(_detail_lines(finding, report_dir))
    friction = [
        finding
        for item in result.results
        for finding in item.findings
        if finding.kind == "friction_path"
    ]
    lines.extend(["", "## Friction (advisory)", ""])
    if not friction:
        lines.append("No friction findings.")
    else:
        for finding in friction:
            workers = ", ".join(finding.worker_ids) or finding.worker_id
            lines.append(
                f"- [{finding.severity}] {finding.title} "
                f"(worker {workers}, backend {finding.backend}) "
                f"-> findings/{finding.id}.md"
            )
    lines.extend(["", "## Workers", ""])
    if not result.results:
        lines.append("No worker results.")
    else:
        seen: list[str] = []
        for item in result.results:
            if item.worker_id in seen:
                continue
            seen.append(item.worker_id)
            lines.append(f"- `{item.worker_id}` backend `{item.backend}`")
    lines.append("")
    return "\n".join(lines)


def empty_result(campaign_id: str, report_dir: Path, backend: str = "local") -> CampaignResult:
    return CampaignResult(
        campaign_id=campaign_id,
        report_dir=str(report_dir),
        backend=backend,
        exit_code=0,
    )


def summary_payload(result: CampaignResult) -> dict:
    return to_plain(result)


def _bullet(finding: Finding) -> str:
    workers = ", ".join(finding.worker_ids) or finding.worker_id
    kind = f", {finding.kind}" if finding.kind in _VISUAL_KINDS else ""
    return (
        f"- [{finding.severity}] {finding.title} "
        f"(worker {workers}, backend {finding.backend}{kind}) "
        f"-> findings/{finding.id}.md"
    )


def _detail_lines(finding: Finding, report_dir: Path | None) -> list[str]:
    """Indented category, confidence, sources, clip, and repro lines under a bullet."""
    head = f"category {finding.category}, confidence {finding.confidence:.2f}"
    if finding.advisory:
        head += ", advisory"
    if finding.environment.get("regressed") == "true":
        head += ", regressed"
    elif finding.environment.get("seen_before") == "true":
        head += f", seen before (first {finding.environment.get('first_seen', '?')})"
    confirmation = confirmation_text(finding)
    if confirmation:
        head += f", {confirmation.lower()}"
    lines = [f"  - {head}"]
    if finding.occurrences:
        shown = ", ".join(finding.occurrences[:10]) + (", ..." if len(finding.occurrences) > 10 else "")
        lines.append(f"  - also seen in {_plural(len(finding.occurrences), 'more place')}: {shown}")
    if finding.suspected_sources:
        lines.append("  - suspected sources: " + ", ".join(f"`{source}`" for source in finding.suspected_sources))
    if finding.evidence.video_clip:
        lines.append(f"  - clip: {finding.evidence.video_clip}")
    if finding.repro:
        replay = resolve(report_dir, finding.repro) if report_dir is not None else Path(finding.repro)
        lines.append(f"  - repro: `{repro_command(replay)}`")
    return lines


def _evidence(findings: list[Finding]) -> str:
    if not findings:
        return ""
    parts: list[str] = []
    for finding in findings:
        parts.append(f"findings/{finding.id}.md")
    return ", ".join(parts)


def _fmt_optional(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:.4f}"
