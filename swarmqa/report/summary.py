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
    lines = [
        f"# Campaign {result.campaign_id}",
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
    findings = [
        finding
        for item in result.results
        for finding in item.findings
        if finding.kind != "friction_path"
    ]
    firm = [finding for finding in findings if not finding.advisory]
    advisory = [finding for finding in findings if finding.advisory]
    lines.extend(["", "## Findings", ""])
    if not firm:
        lines.append("No findings.")
    for finding in firm:
        lines.append(_bullet(finding))
        lines.extend(_detail_lines(finding, report_dir))
    lines.extend(["", "## Advisory findings", ""])
    lines.append("Reported only by a model or a soft heuristic; check before acting.")
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
    lines = [f"  - {head}"]
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
