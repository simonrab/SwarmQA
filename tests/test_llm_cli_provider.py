"""claude_cli and codex_cli: the coding-agent CLIs as model providers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm.cli_provider import CLITransport, ClaudeCLIProvider, CodexCLIProvider
from swarmqa.llm.protocol import ModelRefused, ModelTimeout, Rubric
from swarmqa.llm.settings import LLMSettings, create_provider
from swarmqa.models import UIElement


def _obs(tmp_path: Path, *, shot: bool = False) -> ScreenObservation:
    tree = [UIElement(role="window", label="Demo", children=[UIElement(role="button", label="Save", identifier="save")])]
    path = None
    if shot:
        path = tmp_path / "screen.png"
        Image.new("RGB", (40, 80), "white").save(path)
    return ScreenObservation(tree=tree, ts=1.0, screenshot=path, size=(390, 844))


class FakeRunner:
    """Records each CLI call and answers like the CLI would."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def __call__(self, args, *, timeout=None, input=None, cwd=None, **_):
        self.calls.append({"args": list(args), "input": input, "cwd": cwd})
        return self.answer(args, cwd)


def _claude_envelope(structured, **extra):
    body = {
        "type": "result",
        "is_error": False,
        "result": json.dumps(structured),
        "structured_output": structured,
        "usage": {"input_tokens": 9, "output_tokens": 30, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5},
        "modelUsage": {"claude-sonnet-5-5": {}},
        "total_cost_usd": 0.01,
    }
    body.update(extra)
    return lambda args, cwd: subprocess.CompletedProcess(args, 0, json.dumps(body), "")


def _claude(runner) -> ClaudeCLIProvider:
    return ClaudeCLIProvider(LLMSettings(provider="claude_cli"), CLITransport("claude", runner=runner))


def test_claude_cli_step_decision(tmp_path):
    runner = FakeRunner(_claude_envelope({"kind": "tap", "element_id": "e2", "rationale": "save it", "confidence": 0.9}))
    decision = _claude(runner).decide_step(_obs(tmp_path), "Save the note", [])
    assert decision.kind == "tap" and decision.target.identifier == "save"
    assert decision.usage.provider == "claude_cli" and decision.usage.cost == 0.0
    assert decision.usage.model == "claude-sonnet-5-5"
    args = runner.calls[0]["args"]
    assert args[:4] == ["claude", "-p", "--output-format", "json"]
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--tools") + 1] == ""  # no screenshot: no tools at all
    assert json.loads(args[args.index("--json-schema") + 1])["type"] == "object"
    assert "Save the note" in runner.calls[0]["input"]


def test_claude_cli_judge_reads_the_screenshot(tmp_path):
    seen = {}

    def answer(args, cwd):
        images = list(Path(cwd).glob("screen.*"))
        seen["image"] = images[0].is_file() if images else False
        return _claude_envelope({"issues": [], "summary": "fine"})(args, cwd)

    runner = FakeRunner(answer)
    rubric = Rubric(name="visual", instructions="look for clipped text")
    judgment = _claude(runner).judge_screen(_obs(tmp_path, shot=True), rubric)
    assert judgment.summary == "fine" and seen["image"]
    args = runner.calls[0]["args"]
    assert args[args.index("--tools") + 1] == "Read"
    assert "--add-dir" in args
    assert "Read it before answering" in runner.calls[0]["input"]


def test_claude_cli_error_is_refused(tmp_path):
    runner = FakeRunner(_claude_envelope({}, is_error=True, result="Not logged in", structured_output=None))
    with pytest.raises(ModelRefused, match="Not logged in"):
        _claude(runner).decide_step(_obs(tmp_path), "g", [])


def test_claude_cli_garbage_output_is_a_model_error(tmp_path):
    runner = FakeRunner(lambda args, cwd: subprocess.CompletedProcess(args, 1, "", "command failed: auth"))
    with pytest.raises(Exception, match="claude -p failed"):
        _claude(runner).decide_step(_obs(tmp_path), "g", [])


def test_cli_timeout_is_a_model_timeout(tmp_path):
    def answer(args, cwd):
        raise subprocess.TimeoutExpired(args, 1)

    settings = LLMSettings(provider="claude_cli", max_retries=0)
    provider = ClaudeCLIProvider(settings, CLITransport("claude", runner=FakeRunner(answer)))
    with pytest.raises(ModelTimeout):
        provider.decide_step(_obs(tmp_path), "g", [], timeout_s=2)


def test_codex_cli_step_decision(tmp_path):
    def answer(args, cwd):
        out = Path(args[args.index("--output-last-message") + 1])
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        assert schema["type"] == "object"
        out.write_text(json.dumps({"kind": "done", "element_id": None, "rationale": "goal reached", "confidence": 1}))
        return subprocess.CompletedProcess(args, 0, "", "")

    runner = FakeRunner(answer)
    provider = CodexCLIProvider(LLMSettings(provider="codex_cli"), CLITransport("codex", runner=runner))
    decision = provider.decide_step(_obs(tmp_path), "Save the note", [])
    assert decision.kind == "done"
    args = runner.calls[0]["args"]
    assert args[:2] == ["codex", "exec"] and args[-1] == "-"
    assert "--model" not in args  # "default" leaves Codex's own model
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "Save the note" in runner.calls[0]["input"]


def test_codex_cli_failure_is_a_model_error(tmp_path):
    runner = FakeRunner(lambda args, cwd: subprocess.CompletedProcess(args, 1, "", "error: not signed in"))
    provider = CodexCLIProvider(LLMSettings(provider="codex_cli"), CLITransport("codex", runner=runner))
    with pytest.raises(Exception, match="not signed in"):
        provider.decide_step(_obs(tmp_path), "g", [])


def test_create_provider_builds_the_cli_providers():
    assert isinstance(create_provider(LLMSettings(provider="claude_cli")), ClaudeCLIProvider)
    assert isinstance(create_provider(LLMSettings(provider="codex_cli")), CodexCLIProvider)
