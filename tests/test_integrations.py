"""WP-C3: aqa file-issues, the removed PR loop, and the agent integration files."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from swarmqa.cli import build_parser, main
from swarmqa.models import Finding
from swarmqa.report.findings_json import write_findings_json

ROOT = Path(__file__).resolve().parents[1]


def _empty_config(tmp_path: Path) -> str:
    path = tmp_path / "empty.aqa.toml"
    path.write_text("", encoding="utf-8")
    return str(path)


def _finding(finding_id: str, *, advisory: bool = False) -> Finding:
    return Finding(
        id=finding_id,
        title=f"Broken button {finding_id}",
        severity="high",
        kind="missing_control",
        steps=['click "Save"'],
        fingerprint=f"{finding_id:0<16}"[:16],
        worker_id="w1",
        backend="local",
        advisory=advisory,
        confidence=0.6 if advisory else 1.0,
    )


def _campaign(tmp_path: Path) -> Path:
    campaign = tmp_path / "reports" / "20260101T000000-abc"
    write_findings_json(campaign, campaign.name, [_finding("f1"), _finding("f2", advisory=True)])
    return campaign


def test_file_issues_dry_run_files_nothing(tmp_path: Path, capsys, monkeypatch):
    campaign = _campaign(tmp_path)
    calls: list = []
    import swarmqa.reporter.issues as issues

    monkeypatch.setattr(issues, "create_issues", lambda *a, **k: calls.append(a) or [])
    code = main(
        [
            "file-issues",
            "--report-root",
            str(tmp_path / "reports"),
            "--config",
            _empty_config(tmp_path),
            "--tracker",
            "github",
        ]
    )
    assert code == 0
    out = capsys.readouterr()
    assert "2 finding(s) -> local, github" in out.out
    assert "would file f1" in out.out
    assert "f2 [high] Broken button f2 (advisory)" in out.out
    assert "dry run" in out.out
    assert "issues.github_repo is not set" in out.err
    assert calls == []
    assert not (campaign / "findings").exists()


def test_file_issues_yes_files_through_gh(tmp_path: Path, capsys, monkeypatch):
    campaign = _campaign(tmp_path)
    config = tmp_path / "aqa.config.toml"
    config.write_text('[issues]\ngithub = true\ngithub_repo = "acme/app"\n', encoding="utf-8")
    commands: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = ""

        def __init__(self, url: str):
            self.stdout = url + "\n"

    def fake_run(command, **kwargs):
        commands.append(command)
        return Done(f"https://github.com/acme/app/issues/{len(commands)}")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    code = main(
        [
            "file-issues",
            "--campaign",
            campaign.name,
            "--report-root",
            str(tmp_path / "reports"),
            "--config",
            str(config),
            "--skip-advisory",
            "--yes",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "1 finding(s) -> local, github" in out
    assert "f1 -> github: https://github.com/acme/app/issues/1" in out
    assert len(commands) == 1
    assert commands[0][:5] == ["gh", "issue", "create", "--repo", "acme/app"]
    assert (campaign / "findings" / "f1.md").is_file()


def test_file_issues_without_findings_json(tmp_path: Path, capsys):
    (tmp_path / "reports" / "c1").mkdir(parents=True)
    assert main(["file-issues", "--report-root", str(tmp_path / "reports")]) == 2
    assert "no findings.json" in capsys.readouterr().err


def test_pr_loop_is_gone():
    assert not (ROOT / "swarmqa" / "prloop").exists()
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--pr-mode", "human"])
    template = tomllib.loads((ROOT / "swarmqa" / "templates" / "aqa.config.toml").read_text())
    assert "pr" not in template


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block, _, _ = text[4:].partition("\n---\n")
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, f"bad frontmatter line: {line!r}"
        fields[key.strip()] = value.strip()
    return fields


def test_claude_code_skill_and_mcp_json():
    base = ROOT / "integrations" / "claude-code"
    skill = (base / "skills" / "qa" / "SKILL.md").read_text(encoding="utf-8")
    meta = _frontmatter(skill)
    assert meta["name"] == "qa"
    assert len(meta["description"]) > 40
    for tool in (
        "start_campaign",
        "campaign_status",
        "list_findings",
        "get_finding",
        "verify_fix",
        "verify_status",
    ):
        assert tool in skill
    loop = skill[skill.index("## The loop"):]
    order = [loop.index(t) for t in ("start_campaign", "campaign_status", "list_findings", "get_finding", "verify_fix", "verify_status", "gh pr create")]
    assert order == sorted(order)
    server = json.loads((base / ".mcp.json").read_text())["mcpServers"]["swarmqa"]
    assert server == {"command": "aqa", "args": ["mcp"]}
    assert (base / "README.md").is_file()


def test_codex_config_and_agents_section():
    base = ROOT / "integrations" / "codex"
    config = tomllib.loads((base / "config.toml").read_text(encoding="utf-8"))
    server = config["mcp_servers"]["swarmqa"]
    assert server["command"] == "aqa"
    assert server["args"] == ["mcp"]
    agents = (base / "AGENTS.md").read_text(encoding="utf-8")
    for tool in ("start_campaign", "verify_fix", "verify_status", "gh pr create"):
        assert tool in agents
    assert (base / "README.md").is_file()


def test_integration_tool_names_match_the_mcp_contract():
    from swarmqa.mcp.tools import TOOLS

    names = {tool.__name__ for tool in TOOLS}
    docs = (ROOT / "docs" / "agents.md").read_text(encoding="utf-8")
    for name in names:
        assert f"`{name}(" in docs


@pytest.mark.parametrize(
    "argv",
    [
        ["file-issues", "--config", "MISSING.toml"],
        ["replay", "README.md", "--config", "MISSING.toml"],
    ],
)
def test_an_explicit_missing_config_is_an_error(argv, capsys):
    assert main(argv) == 2
    assert "not found" in capsys.readouterr().err


def test_verify_with_an_explicit_missing_config_is_an_error(capsys):
    from swarmqa.verify.cli import main as verify_main

    assert verify_main(["f-1", "--config", "MISSING.toml"]) == 2
    assert "not found" in capsys.readouterr().err


def test_file_issues_reports_a_failure_on_any_tracker(tmp_path: Path, capsys, monkeypatch):
    from swarmqa.models import IssueRef

    _campaign(tmp_path)
    config = tmp_path / "aqa.config.toml"
    config.write_text(
        '[issues]\ngithub = true\ngithub_repo = "acme/app"\nlinear = true\nlinear_team = "T"\n', encoding="utf-8"
    )
    monkeypatch.setenv("LINEAR_API_KEY", "x")
    import swarmqa.reporter.issues as issues

    def only_github(findings, *args, **kwargs):
        return [IssueRef(tracker="github", identifier="1", url="u", finding_id=f.id) for f in findings]

    monkeypatch.setattr(issues, "create_issues", only_github)
    code = main(["file-issues", "--report-root", str(tmp_path / "reports"), "--config", str(config), "--yes"])
    assert code == 1
    assert "-> linear: not filed" in capsys.readouterr().err
