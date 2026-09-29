"""explorer.engine = "agent": config, worker wiring, and model spend."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarmqa.backends.local import execute_shard
from swarmqa.checks.config import checks_settings
from swarmqa.config import load_config
from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import ConfigError
from swarmqa.models import Shard, UIElement, WorkerResult
from swarmqa.orchestrator import campaign as campaign_mod
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.testing import make_app, sample_config


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "aqa.config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _window(*children: UIElement) -> list[UIElement]:
    return [UIElement(role="window", label="Main", frame=(0, 0, 390, 844), children=list(children))]


def _home() -> list[UIElement]:
    return _window(
        UIElement(role="button", label="Open Settings", identifier="home.settings", frame=(20, 100, 200, 44)),
        UIElement(role="button", label="Refresh", identifier="home.refresh", frame=(20, 200, 200, 44)),
        UIElement(role="button", label="Crash Me", identifier="home.crash", frame=(20, 300, 200, 44)),
    )


def _settings() -> list[UIElement]:
    return _window(UIElement(role="button", label="Back", identifier="settings.back", frame=(0, 40, 60, 44)))


def _factory(target, work_dir):
    driver = FakeDriver(target, work_dir)
    driver.set_tree(_home())
    driver.transitions = {"Open Settings": _settings(), "Back": _home()}
    driver.crash_labels = {"Crash Me"}
    return driver


# Config


def test_engine_defaults_to_legacy_and_is_validated(tmp_path: Path):
    assert load_config(_write(tmp_path, "")).explorer.engine == "legacy"
    assert load_config(_write(tmp_path, '[explorer]\nengine = "agent"\n')).explorer.engine == "agent"
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, '[explorer]\nengine = "turbo"\n'))
    assert exc.value.errors == ["explorer.engine: must be one of legacy, agent"]


def test_checks_table_builds_settings(tmp_path: Path):
    config = load_config(_write(tmp_path, ""))
    settings = checks_settings(config)
    assert settings.functional is not None and settings.layout is not None
    assert settings.layout.platform == "macos"
    assert settings.baseline is None
    assert settings.judge is True

    config = load_config(_write(
        tmp_path,
        '[app]\nplatform = "ios"\n[visual]\nenabled = true\nthreshold = 0.05\n'
        '[checks]\nfunctional = false\nlayout = { contrast = false }\njudge = { rubrics = ["visual"] }\n',
    ))
    settings = checks_settings(config)
    assert settings.functional is None
    assert settings.layout.platform == "ios" and settings.layout.contrast is False
    assert settings.baseline is not None and settings.baseline.threshold == 0.05
    assert settings.judge_settings.rubrics == ["visual"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[checks]\nspelling = true\n", "checks: unknown checks: spelling"),
        ("[checks]\nlayout = { colour = true }\n", "checks: unknown checks.layout settings: colour"),
        ('[checks]\nfunctional = "yes"\n', "checks: checks.functional must be true, false, or a table"),
        ('[checks]\njudge = "false"\n', "checks: checks.judge must be true, false, or a table"),
    ],
)
def test_checks_table_rejects_bad_keys(tmp_path: Path, text: str, error: str):
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, text))
    assert exc.value.errors == [error]


# Worker


def test_agent_engine_runs_the_loop_with_checks(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.explorer.engine = "agent"
    config.explorer.max_steps = 40
    result = execute_shard(
        Shard(id="s1", kind="exploratory", name="crawl"),
        "w1",
        tmp_path / "campaign" / "workers" / "w1",
        config,
        driver_factory=_factory,
        campaign_root=tmp_path / "campaign",
    )
    kinds = {finding.kind for finding in result.findings}
    assert result.status == "failed"
    assert {"crash", "unresponsive"} <= kinds
    assert (tmp_path / "campaign" / "workers" / "w1" / "screen_graph.json").is_file()


def test_legacy_engine_is_unchanged(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    result = execute_shard(
        Shard(id="s1", kind="exploratory", name="crawl", goal="open settings"),
        "w1",
        tmp_path / "campaign" / "workers" / "w1",
        config,
        driver_factory=_factory,
        campaign_root=tmp_path / "campaign",
    )
    assert not (tmp_path / "campaign" / "workers" / "w1" / "screen_graph.json").exists()
    assert result.shard_kind == "exploratory"


def test_missing_llm_extra_fails_before_launch(tmp_path: Path):
    try:
        import anthropic  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("anthropic is installed")
    launched = []

    def factory(target, work_dir):
        launched.append(work_dir)
        return _factory(target, work_dir)

    config = sample_config(make_app(tmp_path))
    config.explorer.engine = "agent"
    config.llm.enabled = True
    config.llm.settings = {"provider": "anthropic"}
    result = execute_shard(
        Shard(id="s1", kind="exploratory", name="crawl"),
        "w1",
        tmp_path / "campaign" / "workers" / "w1",
        config,
        driver_factory=factory,
        campaign_root=tmp_path / "campaign",
    )
    assert result.status == "error"
    assert "llm:" in (result.error or "") and "anthropic" in (result.error or "")
    assert launched == []


def test_fake_llm_provider_is_used_when_enabled(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.explorer.engine = "agent"
    config.llm.enabled = True
    config.llm.settings = {"provider": "fake"}
    result = execute_shard(
        Shard(id="s1", kind="exploratory", name="hunt", goal="look around"),
        "w1",
        tmp_path / "campaign" / "workers" / "w1",
        config,
        driver_factory=_factory,
        campaign_root=tmp_path / "campaign",
    )
    assert result.status in ("passed", "failed")
    assert any(step.message.startswith("model:") for step in result.steps)


# Campaign


def test_campaign_with_agent_engine_reports_planted_bugs(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    config.explorer.engine = "agent"
    result = run_campaign(config, [Shard(id="s1", kind="exploratory", name="crawl")], driver_factory=_factory)
    kinds = {finding.kind for worker in result.results for finding in worker.findings}
    assert {"crash", "unresponsive"} <= kinds
    summary = (Path(result.report_dir) / "summary.md").read_text()
    assert "Crash on home.crash" in summary
    # Default fail_on = "scripted": exploratory findings are reported, not fatal.
    assert result.exit_code == 0
    config.fail_on = "any"
    again = run_campaign(config, [Shard(id="s1", kind="exploratory", name="crawl")], driver_factory=_factory)
    assert again.exit_code != 0


def test_model_spend_meters_a_local_campaign(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.spend.max_spend = 1.0
    meter, metered, note = campaign_mod._open_meter(config, 0.0)
    assert (metered, note, meter.cap) == (False, campaign_mod.LOCAL_SPEND_NOTE, None)

    config.explorer.engine = "agent"
    config.llm.enabled = True
    meter, metered, note = campaign_mod._open_meter(config, 0.0)
    assert metered and note is None and meter.cap == 1.0
    result = WorkerResult(worker_id="w1", shard_id="s1", status="passed", estimated_cost=0.4)
    campaign_mod._settle_spend(meter, metered, 0.0, config, result, 0.0)
    assert meter.spent == pytest.approx(0.4)
    assert result.estimated_cost == pytest.approx(0.4)


def test_two_workers_do_not_overwrite_each_others_findings(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 2
    config.explorer.engine = "agent"
    shards = [Shard(id="s1", kind="exploratory", name="one"), Shard(id="s2", kind="exploratory", name="two")]
    result = run_campaign(config, shards, driver_factory=_factory)
    ids = [finding.id for worker in result.results for finding in worker.findings]
    assert len(ids) == len(set(ids))
    for worker in result.results:
        for finding in worker.findings:
            assert (Path(result.report_dir) / finding.replay_json).is_file()


def test_aqa_replay_reports_whether_a_finding_reproduces(tmp_path: Path, monkeypatch, capsys):
    from swarmqa import cli
    from swarmqa.report import repro as repro_mod

    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    config.explorer.engine = "agent"
    result = run_campaign(config, [Shard(id="s1", kind="exploratory", name="crawl")], driver_factory=_factory)
    crash = next(f for w in result.results for f in w.findings if f.kind == "crash")
    assert crash.repro and crash.evidence.frames
    replay = Path(result.report_dir) / crash.repro

    monkeypatch.setattr(repro_mod, "_default_driver_factory", lambda _config: _factory)
    assert cli.main(["replay", str(replay), "--config", str(tmp_path / "none.toml"), "--app", config.app.path]) == 1
    assert "reproduced" in capsys.readouterr().out

    def fixed(target, work_dir):
        driver = _factory(target, work_dir)
        driver.crash_labels = set()
        return driver

    monkeypatch.setattr(repro_mod, "_default_driver_factory", lambda _config: fixed)
    assert cli.main(["replay", str(replay), "--config", str(tmp_path / "none.toml"), "--app", config.app.path]) == 0
    assert "did not reproduce" in capsys.readouterr().out


def test_campaign_writes_findings_json_and_enriched_summary(tmp_path: Path):
    from swarmqa.report.findings_json import load_findings_json

    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    config.explorer.engine = "agent"
    result = run_campaign(config, [Shard(id="s1", kind="exploratory", name="crawl")], driver_factory=_factory)
    loaded = load_findings_json(Path(result.report_dir) / "findings.json")
    assert {f.kind for f in loaded} >= {"crash", "unresponsive"}
    crash = next(f for f in loaded if f.kind == "crash")
    assert crash.environment["target_identifier"] == "home.crash"
    assert "event_ts" in crash.environment
    assert "aqa replay" in (Path(result.report_dir) / "summary.md").read_text()


def test_session_scoped_checks_report_at_the_end(tmp_path: Path):
    from swarmqa.checks.protocol import CheckIssue
    from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop

    class SessionCheck:
        name = "session"
        per_screen = False

        def __init__(self):
            self.steps = 0

        def run(self, ctx):
            self.steps += 1
            return []

        def finish(self):
            return [CheckIssue(kind="friction_path", category="confusing", title="Took the long way",
                               advisory=True, confidence=0.5)]

    check = SessionCheck()
    config = sample_config(make_app(tmp_path))
    result = run_agent_loop(
        Shard(id="s1", kind="exploratory", name="crawl"),
        _factory(config.app, tmp_path / "w"),
        config,
        checks=[check],
        settings=AgentLoopSettings(mode="crawl", max_steps=10),
        work_dir=tmp_path / "campaign" / "workers" / "w1",
        worker_id="w1",
    )
    assert check.steps > 0
    friction = [f for f in result.findings if f.kind == "friction_path"]
    assert len(friction) == 1 and friction[0].advisory


def test_aqa_replay_exits_2_when_the_app_cannot_launch(tmp_path: Path, capsys):
    from swarmqa import cli

    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    config.explorer.engine = "agent"
    result = run_campaign(config, [Shard(id="s1", kind="exploratory", name="crawl")], driver_factory=_factory)
    crash = next(f for w in result.results for f in w.findings if f.kind == "crash")
    replay = Path(result.report_dir) / crash.repro
    code = cli.main(["replay", str(replay), "--config", str(tmp_path / "none.toml"),
                     "--app", str(tmp_path / "Missing.app")])
    assert code == 2
    assert "could not replay" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("shards", "partial", "expected"),
    [
        ([Shard(id="s1", kind="exploratory", name="ok")], False, True),
        ([Shard(id="s1", kind="exploratory", name="ok")], True, False),
        ([Shard(id="s1", kind="exploratory", name="ok"), Shard(id="s2", kind="suite", name="broken")], False, False),
    ],
)
def test_only_complete_clean_runs_mark_findings_fixed(tmp_path: Path, monkeypatch, shards, partial, expected):
    from swarmqa.models import RunOptions

    seen = {}
    monkeypatch.setattr(
        campaign_mod, "_finalize_findings",
        lambda findings, root, campaign_id, config, *, full_run: seen.setdefault("full_run", full_run),
    )
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    config.explorer.engine = "agent"
    run_campaign(config, shards, driver_factory=_factory, options=RunOptions(partial=partial))
    assert seen["full_run"] is expected
