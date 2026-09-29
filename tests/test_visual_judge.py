"""Appearance judgment: fake provider, command fail-open, and campaign wiring."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from swarmqa.backends.local import execute_shard
from swarmqa.config import load_config
from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import ConfigError
from swarmqa.explorer.exploratory import run_exploratory
from swarmqa.explorer.scripted import run_scripted
from swarmqa.models import (
    Action,
    CampaignResult,
    Finding,
    Shard,
    UIElement,
    WorkerResult,
)
from swarmqa.report.layout import ensure_campaign_layout, worker_dir
from swarmqa.report.summary import render_summary_md
from swarmqa.testing import make_app, sample_config, sample_tree, scripted_shard
from swarmqa.visual.judge import apply_judgment

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
)

_BAD = "The save button overlaps the title"


def _png(directory: Path, name: str = "screen.png") -> Path:
    path = directory / name
    path.write_bytes(_PNG)
    return path


def _enabled(tmp_path: Path, *, provider: str = "fake", judgment: str = ""):
    config = sample_config()
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = provider  # type: ignore[assignment]
    config.visual.judgment.judgment = judgment
    return config, _png(tmp_path)


def _command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_fake_bad_judgment_creates_finding(tmp_path: Path):
    config, shot = _enabled(tmp_path, judgment=_BAD)

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local", name="home"
    )

    assert error is None
    assert finding is not None
    assert finding.kind == "visual_judgment"
    assert finding.severity == "medium"
    assert finding.title == _BAD
    assert finding.details == _BAD
    assert finding.screenshots == [str(shot)]


def test_fake_ok_creates_no_finding(tmp_path: Path):
    for canned in ("", "fine"):
        config, shot = _enabled(tmp_path, judgment=canned)
        finding, error = apply_judgment(
            shot, config, worker_id="w1", shard_id="s1", backend="local"
        )
        assert finding is None
        assert error is None


def test_disabled_judgment_emits_nothing(tmp_path: Path):
    config, shot = _enabled(tmp_path, judgment=_BAD)
    config.visual.judgment.enabled = False

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert finding is None
    assert error is None


def test_long_judgment_is_truncated_in_the_title(tmp_path: Path):
    text = "x" * 200
    config, shot = _enabled(tmp_path, judgment=text)

    finding, _error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert finding is not None
    assert finding.details == text
    assert len(finding.title) == 120
    assert finding.title.endswith("...")
    assert text.startswith(finding.title[:-3].rstrip())


def test_command_bad_judgment_creates_finding(tmp_path: Path):
    config, shot = _enabled(tmp_path, provider="command")
    config.visual.judgment.command = _command(
        "import json; print(json.dumps({'ok': False, 'judgment': 'Crowded toolbar'}))"
    )

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert error is None
    assert finding is not None
    assert finding.details == "Crowded toolbar"


def test_command_ok_creates_no_finding(tmp_path: Path):
    config, shot = _enabled(tmp_path, provider="command")
    marker = tmp_path / "seen.txt"
    config.visual.judgment.command = _command(
        "import json, sys, pathlib; "
        f"pathlib.Path({str(marker)!r}).write_text(sys.argv[-1]); "
        "print(json.dumps({'ok': True}))"
    )

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert finding is None
    assert error is None
    assert marker.read_text(encoding="utf-8") == str(shot)


def test_command_failure_does_not_raise(tmp_path: Path):
    config, shot = _enabled(tmp_path, provider="command")
    config.visual.judgment.command = _command("import sys; raise SystemExit(3)")

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert finding is None
    assert error == "visual judgment failed open: exit 3"
    assert "\n" not in error


def test_command_failure_keeps_existing_error(tmp_path: Path):
    config, shot = _enabled(tmp_path, provider="command")
    config.visual.judgment.command = _command("import sys; raise SystemExit(1)")

    finding, error = apply_judgment(
        shot,
        config,
        worker_id="w1",
        shard_id="s1",
        backend="local",
        error="suite exit 1",
    )

    assert finding is None
    assert error == "suite exit 1"


def test_unparseable_command_output_fails_open(tmp_path: Path):
    config, shot = _enabled(tmp_path, provider="command")
    config.visual.judgment.command = _command("print('nope')")

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert finding is None
    assert error == "visual judgment failed open: unparseable output"


def test_command_timeout_does_not_spin(tmp_path: Path):
    config, shot = _enabled(tmp_path, provider="command")
    config.visual.judgment.timeout_s = 0.2
    config.visual.judgment.command = _command("import time; time.sleep(30)")

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert finding is None
    assert error == "visual judgment failed open: timed out"


def test_command_env_is_used_when_config_command_is_empty(tmp_path: Path, monkeypatch):
    config, shot = _enabled(tmp_path, provider="command")
    config.visual.judgment.command = ""
    monkeypatch.setenv(
        "AQA_VISUAL_JUDGE_COMMAND",
        _command(
            "import json; print(json.dumps({'ok': False, 'judgment': 'Clipped label'}))"
        ),
    )

    finding, error = apply_judgment(
        shot, config, worker_id="w1", shard_id="s1", backend="local"
    )

    assert error is None
    assert finding is not None
    assert finding.details == "Clipped label"


def test_summary_lists_visual_judgment_with_pixel_findings():
    pixel = Finding(
        id="f-pixel",
        title="Visual diff failed: home",
        severity="medium",
        kind="visual",
        steps=["compare home"],
        fingerprint="abc",
        worker_id="w1",
        backend="local",
    )
    judged = Finding(
        id="f-judge",
        title=_BAD,
        severity="medium",
        kind="visual_judgment",
        steps=["judge home"],
        fingerprint="def",
        worker_id="w1",
        backend="local",
        details=_BAD,
    )
    result = CampaignResult(
        campaign_id="c1",
        report_dir="/tmp",
        results=[
            WorkerResult(
                worker_id="w1",
                shard_id="s-vis",
                status="failed",
                findings=[pixel, judged],
                shard_name="visual",
                shard_kind="visual",
                backend="local",
            )
        ],
        backend="local",
    )

    text = render_summary_md(result)
    findings = text.split("## Findings")[1].split("## Friction (advisory)")[0]
    assert "Visual diff failed: home" in findings
    assert _BAD in findings
    assert "visual_judgment" in findings
    assert ", visual)" in findings


def test_config_loads_judgment_section(tmp_path: Path):
    path = tmp_path / "aqa.config.toml"
    path.write_text(
        """
[visual.judgment]
enabled = true
provider = "fake"
judgment = "Overlapping labels"
timeout_s = 12
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.visual.judgment.enabled is True
    assert config.visual.judgment.provider == "fake"
    assert config.visual.judgment.judgment == "Overlapping labels"
    assert config.visual.judgment.timeout_s == 12
    assert config.visual.judgment.command_env == "AQA_VISUAL_JUDGE_COMMAND"


def test_config_accepts_mode_alias(tmp_path: Path):
    path = tmp_path / "aqa.config.toml"
    path.write_text(
        """
[visual.judgment]
enabled = true
mode = "fake"
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.visual.judgment.provider == "fake"


def test_config_rejects_unknown_provider(tmp_path: Path):
    path = tmp_path / "aqa.config.toml"
    path.write_text(
        """
[visual.judgment]
provider = "http"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    assert any(item.startswith("visual.judgment.provider:") for item in exc.value.errors)


def test_scripted_screenshot_records_bad_judgment(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = "fake"
    config.visual.judgment.judgment = _BAD
    root = tmp_path / "reports" / "camp"
    ensure_campaign_layout(root)
    work = worker_dir(root, "w1")
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard([Action(action="screenshot", name="home")])

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "failed"
    assert result.error is None
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind == "visual_judgment"
    assert finding.details == _BAD
    assert finding.title == _BAD
    written = (root / "findings" / f"{finding.id}.md").read_text(encoding="utf-8")
    assert _BAD in written


def test_scripted_fine_judgment_adds_nothing(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = "fake"
    config.visual.judgment.judgment = "fine"
    root = tmp_path / "reports" / "camp"
    ensure_campaign_layout(root)
    work = worker_dir(root, "w1")
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard([Action(action="screenshot", name="home")])

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "passed"
    assert result.findings == []
    assert result.error is None


def test_scripted_command_failure_does_not_raise(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = "command"
    config.visual.judgment.command = _command("import sys; raise SystemExit(2)")
    root = tmp_path / "reports" / "camp"
    ensure_campaign_layout(root)
    work = worker_dir(root, "w1")
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard([Action(action="screenshot", name="home")])

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "passed"
    assert result.findings == []
    assert result.error == "visual judgment failed open: exit 2"


def test_exploratory_saved_screenshot_records_judgment(tmp_path: Path):
    app = make_app(tmp_path)
    app.maturity = "prototype"
    config = sample_config(app)
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = "fake"
    config.visual.judgment.judgment = "Controls are jammed against the edge"
    root = tmp_path / "reports" / "goal"
    work = root / "workers" / "w1"
    driver = FakeDriver(app, work)
    driver.set_tree(
        [
            UIElement(
                role="window",
                label="Sample",
                children=[
                    UIElement(role="button", label="Save"),
                    UIElement(role="textfield", label="Email", value=""),
                ],
            )
        ]
    )
    shard = Shard(
        id="s-1-goal",
        kind="exploratory",
        name="goal",
        goal="Open the Settings gear",
    )

    result = run_exploratory(shard, driver, config, worker_id="w1", work_dir=work)

    judged = [item for item in result.findings if item.kind == "visual_judgment"]
    assert judged
    assert all(item.details == "Controls are jammed against the edge" for item in judged)
    assert any(item.kind == "missing_control" for item in result.findings)
    assert result.error is None
    written = json.loads((root / "findings" / f"{judged[0].id}.replay.json").read_text())
    assert written["version"] == 1


def test_visual_shard_judges_without_a_baseline(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = "fake"
    config.visual.judgment.judgment = "The window is blank"
    work = tmp_path / "workers" / "w1"
    shard = Shard(id="s-vis", kind="visual", name="visual", visual_names=["home"])

    result = execute_shard(
        shard,
        "w1",
        work,
        config,
        driver_factory=lambda target, directory: FakeDriver(target, directory),
    )

    kinds = [item.kind for item in result.findings]
    assert "visual_judgment" in kinds
    assert "visual" in kinds
    judged = next(item for item in result.findings if item.kind == "visual_judgment")
    assert judged.details == "The window is blank"
    assert result.error is None
    assert (tmp_path / "findings" / f"{judged.id}.md").is_file()


def test_visual_shard_command_failure_does_not_raise(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.visual.judgment.enabled = True
    config.visual.judgment.provider = "command"
    config.visual.judgment.command = _command("import sys; raise SystemExit(4)")
    work = tmp_path / "workers" / "w1"
    shard = Shard(id="s-vis", kind="visual", name="visual", visual_names=["home"])

    result = execute_shard(
        shard,
        "w1",
        work,
        config,
        driver_factory=lambda target, directory: FakeDriver(target, directory),
    )

    assert result.status == "failed"
    assert [item.kind for item in result.findings] == ["visual"]
    assert result.error == "visual judgment failed open: exit 4"
