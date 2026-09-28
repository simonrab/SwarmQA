"""Markdown and JSON campaign summaries.

Orchestrator and the empty-campaign path both call these writers so the
on-disk schema stays one shape.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.models import CampaignResult, Finding
from swarmqa.serialize import dump_json, to_plain


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
    lines.extend(["", "## Findings", ""])
    findings = [
        finding
        for item in result.results
        for finding in item.findings
        if finding.kind != "friction_path"
    ]
    if not findings:
        lines.append("No findings.")
    else:
        for finding in findings:
            workers = ", ".join(finding.worker_ids) or finding.worker_id
            lines.append(
                f"- [{finding.severity}] {finding.title} "
                f"(worker {workers}, backend {finding.backend}) "
                f"-> findings/{finding.id}.md"
            )
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
