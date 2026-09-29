"""Finding artifacts, issue templates, and tracker adapters."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from swarmqa.models import Finding
from swarmqa.reporter.findings import (
    default_template,
    fingerprint_for,
    render_issue,
    write_finding,
    write_replay,
)
from swarmqa.reporter.issues import create_issues
from swarmqa.testing import sample_config

_GH_TOKEN = "ghp_super_secret_token_value"
_LINEAR_TOKEN = "lin_api_super_secret_value"


def _finding(**overrides) -> Finding:
    data = dict(
        id="f1",
        title="Missing control: Save",
        severity="high",
        kind="missing_control",
        steps=['click "Save"', 'click "Not There"'],
        fingerprint="abc123abc123abcd",
        worker_id="w1",
        backend="local",
        shard_id="s-1-save",
        screenshots=["workers/w1/media/failure-0.png"],
        video="workers/w1/media/session.mp4",
        replay_json="findings/f1.replay.json",
        environment={
            "path": "/tmp/Sample.app",
            "bundle_id": "dev.swarmqa.sample",
            "version": "1.2.3",
        },
        details="element not found",
    )
    data.update(overrides)
    return Finding(**data)


def test_fingerprint_is_sha256_prefix():
    assert fingerprint_for("crash", "  App Crashed ", " Button ") == "1c3733ca2a992a59"
    assert fingerprint_for("missing_control", "Missing control: Save", "Save") == "2b98504f6cbd4fd8"
    assert fingerprint_for("launch", "App failed to launch", "") == "471bd5f602239a90"
    assert fingerprint_for("Crash", "t", "x") != fingerprint_for("crash", "t", "x")
    assert len(fingerprint_for("timeout", "Timed out", "Email")) == 16


def test_render_issue_fills_known_placeholders_and_keeps_unknown_tokens():
    finding = _finding(title="has {{severity}} in title", video=None, replay_json=None)
    text = render_issue(
        "{{title}}|{{severity}}|{{kind}}|{{steps}}|{{video}}|{{screenshots}}|"
        "{{replay_json}}|{{environment}}|{{worker_id}}|{{backend}}|{{build_id}}|"
        "{{fingerprint}}|{{details}}|{{not_a_token}}",
        finding,
    )
    assert text == (
        "has {{severity}} in title|high|missing_control|"
        'click "Save"\nclick "Not There"||workers/w1/media/failure-0.png||'
        "path: /tmp/Sample.app\nbundle_id: dev.swarmqa.sample\nversion: 1.2.3|"
        "w1|local|1.2.3|abc123abc123abcd|element not found|{{not_a_token}}"
    )


def test_render_issue_empty_environment_has_empty_build_id():
    finding = _finding(environment={}, video="workers/w1/media/session.mp4")
    assert render_issue("{{environment}}|{{build_id}}|{{video}}", finding) == (
        "||workers/w1/media/session.mp4"
    )
    assert render_issue("[{{video}}]", _finding(video=None)) == "[]"


def test_default_template_reads_package_file():
    path = Path(__file__).resolve().parents[1] / "swarmqa" / "templates" / "issue.md"
    assert default_template() == path.read_text(encoding="utf-8")
    assert "{{title}}" in default_template()
    assert "{{replay_json}}" in default_template()


def test_write_finding_and_replay(tmp_path: Path):
    finding = _finding()
    markdown = write_finding(finding, tmp_path)
    assert markdown == tmp_path / "findings" / "f1.md"
    text = markdown.read_text(encoding="utf-8")
    assert "# Missing control: Save" in text
    assert "Severity: high" in text
    assert 'click "Save"' in text
    assert "workers/w1/media/failure-0.png" in text
    assert "workers/w1/media/session.mp4" in text
    assert "Worker: w1" in text
    assert "Backend: local" in text
    assert "Build: 1.2.3" in text

    replay = write_replay(
        "f1",
        tmp_path,
        [{"action": "click", "text": None, "target": {"label": "Save", "role": None}}],
    )
    assert replay == tmp_path / "findings" / "f1.replay.json"
    payload = json.loads(replay.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["name"] == "f1"
    assert payload["steps"] == [{"action": "click", "target": {"label": "Save"}}]


def test_create_issues_always_writes_a_local_ticket(tmp_path: Path):
    finding = _finding()
    config = sample_config()
    called = {"runner": False}

    def runner(*_args, **_kwargs):
        called["runner"] = True
        raise AssertionError("runner should not be called")

    refs = create_issues([finding], config, tmp_path, runner=runner)
    assert called["runner"] is False
    assert len(refs) == 1
    assert refs[0].tracker == "local"
    assert refs[0].identifier == "f1"
    assert refs[0].finding_id == "f1"
    text = (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8")
    assert "Missing control: Save" in text
    assert "element not found" in text


def test_create_issues_uses_custom_template(tmp_path: Path):
    template = tmp_path / "ticket.md"
    template.write_text("TICKET {{title}} {{not_a_token}}\n", encoding="utf-8")
    config = sample_config()
    config.issues.template = str(template)
    refs = create_issues([_finding()], config, tmp_path)
    assert refs[0].tracker == "local"
    assert (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8") == (
        "TICKET Missing control: Save {{not_a_token}}\n"
    )


def test_github_issue_includes_evidence_and_hides_the_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SWARMQA_TEST_GH", _GH_TOKEN)
    finding = _finding()
    write_replay(finding.id, tmp_path, [{"action": "click", "target": {"label": "Save"}}])
    config = sample_config()
    config.issues.github = True
    config.issues.github_repo = "acme/app"
    config.issues.github_token_env = "SWARMQA_TEST_GH"
    captured: dict = {}

    def runner(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(
            returncode=0,
            stdout="https://github.com/acme/app/issues/42\n",
            stderr="",
        )

    refs = create_issues([finding], config, tmp_path, runner=runner)

    assert captured["args"][:6] == ["gh", "issue", "create", "--repo", "acme/app", "--title"]
    assert captured["args"][6] == finding.title
    assert captured["args"][7] == "--body"
    body = captured["args"][8]
    assert finding.video in body
    assert finding.screenshots[0] in body
    assert "```json" in body
    assert '"version": 1' in body
    assert _GH_TOKEN not in body
    assert captured["env"]["GH_TOKEN"] == _GH_TOKEN
    assert _GH_TOKEN not in " ".join(captured["args"][:8])
    github = [ref for ref in refs if ref.tracker == "github"]
    assert github == [
        type(refs[0])(
            tracker="github",
            identifier="42",
            url="https://github.com/acme/app/issues/42",
            finding_id="f1",
        )
    ]
    assert any(ref.tracker == "local" for ref in refs)
    _assert_token_absent(tmp_path, _GH_TOKEN)


def test_github_failure_is_recorded_and_does_not_raise(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SWARMQA_TEST_GH", _GH_TOKEN)
    config = sample_config()
    config.issues.github = True
    config.issues.github_repo = "acme/app"
    config.issues.github_token_env = "SWARMQA_TEST_GH"
    finding = _finding()

    def runner(*_args, **_kwargs):
        raise RuntimeError(f"gh exploded {_GH_TOKEN}")

    refs = create_issues([finding], config, tmp_path, runner=runner)

    assert [ref.tracker for ref in refs] == ["local"]
    text = (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8")
    assert "github:" in text
    assert "gh exploded" in text
    assert _GH_TOKEN not in text
    assert "[redacted]" in text
    assert "github:" in finding.details
    _assert_token_absent(tmp_path, _GH_TOKEN)


def test_github_without_repo_does_not_call_runner(tmp_path: Path):
    config = sample_config()
    config.issues.github = True
    config.issues.github_repo = None

    def runner(*_args, **_kwargs):
        raise AssertionError("runner should not be called")

    refs = create_issues([_finding()], config, tmp_path, runner=runner)
    assert [ref.tracker for ref in refs] == ["local"]
    assert "github_repo" in (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8")


def test_linear_issue_posts_graphql(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SWARMQA_TEST_LINEAR", _LINEAR_TOKEN)
    finding = _finding()
    write_replay(finding.id, tmp_path, [{"action": "click", "target": {"label": "Save"}}])
    config = sample_config()
    config.issues.linear = True
    config.issues.linear_team = "team-123"
    config.issues.linear_api_key_env = "SWARMQA_TEST_LINEAR"
    captured: dict = {}

    def http_post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return {
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": "uuid-1",
                        "identifier": "QA-7",
                        "url": "https://linear.app/acme/issue/QA-7",
                    },
                }
            }
        }

    refs = create_issues([finding], config, tmp_path, http_post=http_post)

    assert captured["url"] == "https://api.linear.app/graphql"
    assert captured["headers"]["Authorization"] == _LINEAR_TOKEN
    assert captured["headers"]["Content-Type"] == "application/json"
    assert "issueCreate" in captured["body"]["query"]
    created = captured["body"]["variables"]["input"]
    assert created["teamId"] == "team-123"
    assert created["title"] == finding.title
    description = created["description"]
    assert finding.video in description
    assert finding.screenshots[0] in description
    assert "```json" in description
    assert '"version": 1' in description
    assert _LINEAR_TOKEN not in description
    linear = next(ref for ref in refs if ref.tracker == "linear")
    assert linear.identifier == "QA-7"
    assert linear.url == "https://linear.app/acme/issue/QA-7"
    assert linear.finding_id == "f1"
    assert any(ref.tracker == "local" for ref in refs)
    _assert_token_absent(tmp_path, _LINEAR_TOKEN)


def test_linear_failure_is_recorded_and_does_not_raise(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SWARMQA_TEST_LINEAR", _LINEAR_TOKEN)
    config = sample_config()
    config.issues.linear = True
    config.issues.linear_team = "team-123"
    config.issues.linear_api_key_env = "SWARMQA_TEST_LINEAR"

    def http_post(_url, _headers, _body):
        raise RuntimeError(f"linear down {_LINEAR_TOKEN}")

    finding = _finding()
    refs = create_issues([finding], config, tmp_path, http_post=http_post)
    assert [ref.tracker for ref in refs] == ["local"]
    text = (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8")
    assert "linear:" in text
    assert _LINEAR_TOKEN not in text
    _assert_token_absent(tmp_path, _LINEAR_TOKEN)


def test_linear_without_key_does_not_post(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("SWARMQA_TEST_LINEAR", raising=False)
    config = sample_config()
    config.issues.linear = True
    config.issues.linear_team = "team-123"
    config.issues.linear_api_key_env = "SWARMQA_TEST_LINEAR"

    def http_post(*_args, **_kwargs):
        raise AssertionError("http_post should not be called")

    refs = create_issues([_finding()], config, tmp_path, http_post=http_post)
    assert [ref.tracker for ref in refs] == ["local"]
    assert "SWARMQA_TEST_LINEAR" in (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8")


def test_github_and_linear_refs_follow_the_local_ticket(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SWARMQA_TEST_GH", _GH_TOKEN)
    monkeypatch.setenv("SWARMQA_TEST_LINEAR", _LINEAR_TOKEN)
    config = sample_config()
    config.issues.github = True
    config.issues.github_repo = "acme/app"
    config.issues.github_token_env = "SWARMQA_TEST_GH"
    config.issues.linear = True
    config.issues.linear_team = "team-123"
    config.issues.linear_api_key_env = "SWARMQA_TEST_LINEAR"

    def runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="https://github.com/acme/app/issues/3\n", stderr="")

    def http_post(*_args, **_kwargs):
        return {
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {"identifier": "QA-1", "url": "https://linear.app/acme/issue/QA-1"},
                }
            }
        }

    refs = create_issues([_finding()], config, tmp_path, runner=runner, http_post=http_post)
    assert [ref.tracker for ref in refs] == ["local", "github", "linear"]


def test_missing_template_falls_back_to_default(tmp_path: Path):
    config = sample_config()
    config.issues.template = str(tmp_path / "missing-template.md")
    create_issues([_finding()], config, tmp_path)
    text = (tmp_path / "findings" / "f1.md").read_text(encoding="utf-8")
    assert text.startswith("# Missing control: Save")


def test_create_issues_empty_list():
    assert create_issues([], sample_config(), Path(".")) == []


def _assert_token_absent(root: Path, token: str) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(encoding="utf-8")


def test_v2_placeholders_and_triage_section(tmp_path: Path):
    from swarmqa.models import Evidence

    finding = _finding(
        advisory=True,
        confidence=0.4,
        repro="findings/f1.replay.json",
        suspected_sources=["App/Save.swift:3"],
        evidence=Evidence(video_clip="media/f1.clip.mp4", frames=["media/a.png"]),
    )
    text = render_issue("{{category}}|{{confidence}}|{{advisory}}|{{repro}}|{{video_clip}}|{{frames}}|{{suspected_sources}}", finding)
    assert text == "broken|0.40|true|findings/f1.replay.json|media/f1.clip.mp4|media/a.png|App/Save.swift:3"
    markdown = write_finding(finding, tmp_path).read_text(encoding="utf-8")
    assert "## Triage" in markdown
    assert "Advisory: yes (model-only)" in markdown
    assert "- App/Save.swift:3" in markdown
    assert "Clip: media/f1.clip.mp4" in markdown
