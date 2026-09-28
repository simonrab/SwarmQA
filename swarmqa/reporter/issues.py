"""C4 — GitHub and Linear issue creation.

Always write local ticket files even when trackers are disabled.
See docs/CONTRACTS.md section C4.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from swarmqa.models import CampaignConfig, Finding, IssueRef
from swarmqa.reporter.findings import default_template, render_issue, write_finding

_LINEAR_URL = "https://api.linear.app/graphql"
_LINEAR_QUERY = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      url
    }
  }
}
""".strip()
_URL = re.compile(r"https?://[^\s]+")


def create_issues(
    findings: list[Finding],
    config: CampaignConfig,
    campaign_dir: Path,
    *,
    runner: Callable | None = None,
    http_post: Callable | None = None,
) -> list[IssueRef]:
    """Create tracker issues when enabled. Include local IssueRefs always."""
    campaign_dir = Path(campaign_dir)
    invoke = runner or subprocess.run
    template = _load_template(config)
    secrets = _secret_values(config)
    refs: list[IssueRef] = []
    for finding in findings:
        refs.append(_write_local_ticket(finding, campaign_dir, template, secrets))
        if config.issues.github:
            github_ref = _create_github_issue(
                finding, config, campaign_dir, template, invoke, secrets
            )
            if github_ref is not None:
                refs.append(github_ref)
        if config.issues.linear:
            linear_ref = _create_linear_issue(
                finding, config, campaign_dir, template, http_post, secrets
            )
            if linear_ref is not None:
                refs.append(linear_ref)
    return refs


def _load_template(config: CampaignConfig) -> str:
    configured = config.issues.template
    if configured:
        path = Path(configured)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return default_template()


def _write_local_ticket(
    finding: Finding,
    campaign_dir: Path,
    template: str,
    secrets: list[str],
) -> IssueRef:
    path = campaign_dir / "findings" / f"{finding.id}.md"
    if not path.exists():
        write_finding(finding, campaign_dir)
    _write_rendered(path, template, finding, secrets)
    return IssueRef(tracker="local", identifier=finding.id, url=None, finding_id=finding.id)


def _write_rendered(path: Path, template: str, finding: Finding, secrets: list[str]) -> None:
    text = render_issue(template, finding)
    if not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_scrub(text, secrets), encoding="utf-8")


def _tracker_body(finding: Finding, campaign_dir: Path, template: str, secrets: list[str]) -> str:
    rendered = render_issue(template, finding)
    parts = [rendered.rstrip(), ""]
    if finding.video and finding.video not in rendered:
        parts.append(f"Video: {finding.video}")
    missing_shots = [shot for shot in finding.screenshots if shot not in rendered]
    if missing_shots:
        parts.append("Screenshots:")
        parts.extend(missing_shots)
    if finding.replay_json and finding.replay_json not in rendered:
        parts.append(f"Replay JSON: {finding.replay_json}")
    parts.append("```json")
    parts.append(_replay_stub(finding, campaign_dir).rstrip("\n"))
    parts.append("```")
    parts.append("")
    return _scrub("\n".join(parts), secrets)


def _replay_stub(finding: Finding, campaign_dir: Path) -> str:
    path = _resolve_replay(finding.replay_json, campaign_dir)
    if path is not None and path.is_file():
        return path.read_text(encoding="utf-8")
    return json.dumps(
        {"name": finding.id or "replay", "steps": [], "version": 1},
        indent=2,
        sort_keys=True,
    ) + "\n"


def _resolve_replay(value: str | None, campaign_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = campaign_dir / path
    return path


def _create_github_issue(
    finding: Finding,
    config: CampaignConfig,
    campaign_dir: Path,
    template: str,
    runner: Callable,
    secrets: list[str],
) -> IssueRef | None:
    try:
        repo = config.issues.github_repo
        if not repo:
            raise RuntimeError("issues.github_repo is not set")
        body = _tracker_body(finding, campaign_dir, template, secrets)
        command = [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            finding.title,
            "--body",
            body,
        ]
        env = os.environ.copy()
        token_var = config.issues.github_token_env or "GH_TOKEN"
        token = os.environ.get(token_var)
        if token:
            env["GH_TOKEN"] = token
        result = runner(
            command,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        returncode, stdout, stderr = _coerce_process(result)
        if returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit {returncode}"
            raise RuntimeError(f"gh issue create failed: {detail}")
        url = _extract_url(stdout)
        if not url:
            raise RuntimeError("gh issue create returned no URL")
        return IssueRef(
            tracker="github",
            identifier=_identifier_from_url(url),
            url=url,
            finding_id=finding.id,
        )
    except Exception as exc:
        _record_tracker_failure(finding, campaign_dir, template, "github", exc, secrets)
        return None


def _create_linear_issue(
    finding: Finding,
    config: CampaignConfig,
    campaign_dir: Path,
    template: str,
    http_post: Callable | None,
    secrets: list[str],
) -> IssueRef | None:
    try:
        if http_post is None:
            raise RuntimeError("linear http_post is not configured")
        team = config.issues.linear_team
        if not team:
            raise RuntimeError("issues.linear_team is not set")
        key_var = config.issues.linear_api_key_env or "LINEAR_API_KEY"
        token = os.environ.get(key_var)
        if not token:
            raise RuntimeError(f"{key_var} is not set")
        description = _tracker_body(finding, campaign_dir, template, secrets)
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }
        body = {
            "query": _LINEAR_QUERY,
            "variables": {
                "input": {
                    "teamId": team,
                    "title": finding.title,
                    "description": description,
                }
            },
        }
        response = http_post(_LINEAR_URL, headers, body)
        identifier, url = _parse_linear(response)
        return IssueRef(
            tracker="linear",
            identifier=identifier,
            url=url,
            finding_id=finding.id,
        )
    except Exception as exc:
        _record_tracker_failure(finding, campaign_dir, template, "linear", exc, secrets)
        return None


def _parse_linear(response) -> tuple[str, str | None]:
    if not isinstance(response, dict):
        raise RuntimeError("linear response was not an object")
    if response.get("errors"):
        raise RuntimeError(_linear_error_text(response))
    payload = (response.get("data") or {}).get("issueCreate") or {}
    if payload.get("success") is False:
        raise RuntimeError("linear issueCreate was not successful")
    issue = payload.get("issue") or {}
    identifier = issue.get("identifier") or issue.get("id")
    if not identifier:
        raise RuntimeError("linear issueCreate returned no issue identifier")
    url = issue.get("url")
    return str(identifier), str(url) if url else None


def _linear_error_text(response: dict) -> str:
    messages: list[str] = []
    for item in response.get("errors") or []:
        if isinstance(item, dict) and item.get("message"):
            messages.append(str(item["message"]))
        elif isinstance(item, str):
            messages.append(item)
    return "; ".join(messages) or "linear request failed"


def _record_tracker_failure(
    finding: Finding,
    campaign_dir: Path,
    template: str,
    tracker: str,
    exc: Exception,
    secrets: list[str],
) -> None:
    message = _scrub(f"{tracker}: {exc}", secrets)
    finding.details = f"{finding.details}\n{message}".strip() if finding.details else message
    path = campaign_dir / "findings" / f"{finding.id}.md"
    text = render_issue(template, finding)
    if message not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n## Tracker errors\n\n{message}\n"
    elif not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_scrub(text, secrets), encoding="utf-8")


def _coerce_process(result) -> tuple[int, str, str]:
    if isinstance(result, str):
        return 0, result, ""
    returncode = getattr(result, "returncode", 0)
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if returncode is None:
        returncode = 0
    return int(returncode), str(stdout), str(stderr)


def _extract_url(stdout: str) -> str | None:
    matches = _URL.findall(stdout or "")
    if not matches:
        return None
    return matches[-1].rstrip(").,]>\"'")


def _identifier_from_url(url: str) -> str:
    path = url.split("?", 1)[0].rstrip("/")
    identifier = path.rsplit("/", 1)[-1]
    return identifier or url


def _secret_values(config: CampaignConfig) -> list[str]:
    names = [
        config.issues.github_token_env,
        config.issues.linear_api_key_env,
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "LINEAR_API_KEY",
    ]
    secrets: list[str] = []
    for name in names:
        if not name:
            continue
        value = os.environ.get(name)
        if value and value not in secrets and len(value) >= 4:
            secrets.append(value)
    return secrets


def _scrub(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        redacted = redacted.replace(secret, "[redacted]")
    return redacted
