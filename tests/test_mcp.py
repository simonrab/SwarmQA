from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarmqa.errors import AQAError, ConfigError, IntentError
from swarmqa.mcp import _runner, campaigns, tools
from swarmqa.mcp.tools import CampaignHandle, VerifyResult
from swarmqa.models import ActiveWorker, CampaignStatus, Evidence, Finding
from swarmqa.orchestrator.status import load_status, write_status
from swarmqa.report.findings_json import write_findings_json
from swarmqa.report.layout import ensure_campaign_layout
from swarmqa.serialize import dump_json
from swarmqa.testing import make_app


@pytest.fixture
def project(tmp_path: Path):
    """A project directory the MCP tools treat as their root."""
    campaigns.configure(root=tmp_path)
    yield tmp_path
    campaigns.configure()


def _finding(finding_id: str, severity: str = "high", **kwargs) -> Finding:
    return Finding(
        id=finding_id,
        title=kwargs.pop("title", f"finding {finding_id}"),
        severity=severity,  # type: ignore[arg-type]
        kind=kwargs.pop("kind", "assertion"),  # type: ignore[arg-type]
        steps=["tap Save"],
        fingerprint=kwargs.pop("fingerprint", f"fp-{finding_id}"),
        worker_id="w1",
        backend="local",
        **kwargs,
    )


def _campaign(root: Path, campaign_id: str, *, state: str = "finished", findings=None, meta=None) -> Path:
    path = root / "reports" / campaign_id
    ensure_campaign_layout(path)
    write_status(
        CampaignStatus(campaign_id=campaign_id, state=state, backend="local", workers_configured=2, report_dir=str(path)),
        path,
    )
    if findings is not None:
        write_findings_json(path, campaign_id, findings)
    if meta is not None:
        campaigns.write_meta(path, meta)
    return path


def _write_config(project: Path, **tables: str) -> Path:
    make_app(project)
    body = {
        "app": 'path = "Sample.app"',
        "campaign": "workers = 1",
        "driver": 'kind = "fake"',
        "coverage": "visual = false\nexploratory = false",
        "suite": f"command = \"{sys.executable} -c 'raise SystemExit(3)'\"",
    }
    body.update(tables)
    text = "\n\n".join(f"[{name}]\n{value}" for name, value in body.items()) + "\n"
    path = project / "aqa.config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _wait_done(campaign_id: str, timeout_s: float = 60.0) -> CampaignStatus:
    deadline = time.monotonic() + timeout_s
    while True:
        status = tools.campaign_status(campaign_id)
        if status.state != "running":
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"campaign {campaign_id} still running: {status}")
        time.sleep(0.1)


# campaign_status


def test_campaign_status_latest_and_by_id(project: Path):
    _campaign(project, "20260101T000000Z-aaaaaa")
    _campaign(project, "20260102T000000Z-bbbbbb", state="running")
    assert tools.campaign_status().campaign_id == "20260102T000000Z-bbbbbb"
    assert tools.campaign_status().state == "running"
    first = tools.campaign_status("20260101T000000Z-aaaaaa")
    assert first.state == "finished" and first.workers_configured == 2


def test_campaign_status_errors(project: Path):
    with pytest.raises(AQAError, match="no campaigns"):
        tools.campaign_status()
    _campaign(project, "20260101T000000Z-aaaaaa")
    with pytest.raises(AQAError, match="unknown campaign 'nope'"):
        tools.campaign_status("nope")


def test_campaign_status_before_status_json_and_after_runner_death(project: Path):
    path = project / "reports" / "20260101T000000Z-aaaaaa"
    ensure_campaign_layout(path)
    campaigns.write_meta(path, {"campaign_id": path.name, "backend": "local", "workers": 3, "shards": 4, "pid": os.getpid()})
    status = tools.campaign_status(path.name)
    assert (status.state, status.workers_configured, status.queue_depth) == ("running", 3, 4)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    campaigns.update_meta(path, pid=dead.pid)
    with pytest.raises(AQAError, match="mcp-runner.log"):
        tools.campaign_status(path.name)

    write_status(CampaignStatus(campaign_id=path.name, state="running", backend="local", workers_configured=3,
                                active_workers=[ActiveWorker("w1", "s1", "a", "scripted", "tap")]), path)
    status = tools.campaign_status(path.name)
    assert status.state == "stopped" and status.active_workers == []


def test_report_root_follows_config(project: Path):
    _write_config(project, campaign='workers = 1\nreport_root = "out/qa"')
    _campaign(project, "20260101T000000Z-aaaaaa")  # under reports/, not out/qa
    assert campaigns.report_root() == project / "out" / "qa"
    with pytest.raises(AQAError, match="no campaigns"):
        tools.campaign_status()


# list_findings / get_finding


def _findings_campaign(project: Path) -> Path:
    findings = [
        _finding("low-1", "low", confidence=1.0),
        _finding("crit-1", "critical", confidence=0.6),
        _finding("high-model", "high", confidence=0.4, advisory=True, kind="visual_judgment", category="confusing"),
        _finding("high-firm", "high", confidence=0.9, repro="findings/high-firm.replay.json"),
        _finding("med-1", "medium", replay_json="findings/med-1.replay.json"),
        _finding("crit-2", "critical", confidence=0.95, evidence=Evidence(frames=["media/a.png"])),
    ]
    return _campaign(project, "20260101T000000Z-aaaaaa", findings=findings)


def test_list_findings_sorted_by_severity_then_confidence(project: Path):
    _findings_campaign(project)
    items = tools.list_findings()
    assert [item.id for item in items] == ["crit-2", "crit-1", "high-firm", "high-model", "med-1", "low-1"]
    assert all(item.campaign_id == "20260101T000000Z-aaaaaa" for item in items)
    by_id = {item.id: item for item in items}
    assert by_id["high-firm"].repro == "findings/high-firm.replay.json"
    assert by_id["med-1"].repro == "findings/med-1.replay.json"
    assert by_id["high-model"].advisory and by_id["high-model"].category == "confusing"


def test_list_findings_filters(project: Path):
    _findings_campaign(project)
    assert [i.id for i in tools.list_findings(min_severity="high")] == ["crit-2", "crit-1", "high-firm", "high-model"]
    assert [i.id for i in tools.list_findings(min_severity="critical", include_advisory=False)] == ["crit-2", "crit-1"]
    assert "high-model" not in {i.id for i in tools.list_findings(include_advisory=False)}
    with pytest.raises(AQAError, match="unknown severity"):
        tools.list_findings(min_severity="urgent")  # type: ignore[arg-type]


def test_list_findings_by_pr(project: Path):
    _findings_campaign(project)
    assert tools.list_findings(pr=12) == []
    _campaign(project, "20260102T000000Z-bbbbbb", findings=[_finding("pr-1")], meta={"pr": 12})
    _campaign(project, "20260103T000000Z-cccccc", findings=[_finding("other")])
    assert [i.id for i in tools.list_findings(pr=12)] == ["pr-1"]
    assert tools.list_findings(pr=12, campaign_id="20260101T000000Z-aaaaaa") == []


def test_list_findings_while_running_reads_worker_results(project: Path):
    path = _campaign(project, "20260101T000000Z-aaaaaa", state="running")
    dump_json({"findings": [_finding("a"), _finding("a-dup", fingerprint="fp-a"), _finding("b", "critical")]},
              path / "workers" / "w1" / "result.json")
    assert [i.id for i in tools.list_findings()] == ["b", "a"]


def test_get_finding(project: Path):
    _findings_campaign(project)
    _campaign(project, "20260102T000000Z-bbbbbb", findings=[_finding("newer")])
    finding = tools.get_finding("crit-2")
    assert isinstance(finding, Finding)
    assert finding.evidence.frames == ["media/a.png"] and finding.confidence == 0.95
    assert tools.get_finding("newer").id == "newer"
    with pytest.raises(AQAError, match="unknown finding 'crit-2' in campaign 20260102T000000Z-bbbbbb"):
        tools.get_finding("crit-2", campaign_id="20260102T000000Z-bbbbbb")
    with pytest.raises(AQAError, match="list_findings"):
        tools.get_finding("missing")


# verify_fix / verify_status


def test_verify_delegates_to_swarmqa_verify(project: Path, monkeypatch: pytest.MonkeyPatch):
    from swarmqa import verify

    _campaign(project, "20260101T000000Z-aaaaaa")
    calls: list = []

    def fake_start(finding_id, *, config_path, campaign_id, devices, build):
        calls.append((finding_id, Path(config_path), campaign_id, devices, build))
        return VerifyResult(verify_id="v1", finding_id=finding_id, state="running", devices=devices)

    def fake_status(verify_id, *, report_root=None):
        calls.append((verify_id, Path(report_root)))
        return VerifyResult(verify_id=verify_id, finding_id="f1", state="passed", devices=2)

    monkeypatch.setattr(verify, "start_verify", fake_start)
    monkeypatch.setattr(verify, "verify_status", fake_status)
    started = tools.verify_fix("f1", campaign_id="20260101T000000Z-aaaaaa", devices=3, build=False)
    assert (started.verify_id, started.state, started.devices) == ("v1", "running", 3)
    assert tools.verify_status("v1").state == "passed"
    assert calls == [
        ("f1", project / "aqa.config.toml", "20260101T000000Z-aaaaaa", 3, False),
        ("v1", project / "reports"),
    ]
    with pytest.raises(AQAError, match="unknown campaign"):
        tools.verify_fix("f1", campaign_id="nope")
    with pytest.raises(AQAError, match="devices"):
        tools.verify_fix("f1", devices=0)


# start_campaign


def test_start_campaign_prepares_dir_and_launches(project: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(project)
    launched: list = []
    monkeypatch.setattr(campaigns, "launch_runner", lambda campaign, cwd: launched.append((campaign, cwd)) or 4242)
    handle = tools.start_campaign(workers=2, platform="macos", sha="abc123")
    assert isinstance(handle, CampaignHandle)
    campaign = Path(handle.report_dir)
    assert campaign == (project / "reports" / handle.campaign_id).resolve()
    assert launched == [(campaign, project)]
    meta = campaigns.read_meta(campaign)
    assert meta["pid"] == 4242 and meta["sha"] == "abc123" and meta["workers"] == 2 and meta["shards"] == 1
    assert meta["request"]["platform"] == "macos"
    assert (campaign / "raw").is_dir()
    # Rebuilding the request gives the same config the runner will use.
    config = campaigns.load_request_config(campaigns.CampaignRequest.from_meta(meta))
    assert config.workers == 2 and Path(config.report_root) == project / "reports"


def test_start_campaign_errors(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(campaigns, "launch_runner", lambda campaign, cwd: pytest.fail("must not launch"))
    with pytest.raises(AQAError, match="config not found"):
        tools.start_campaign()
    _write_config(project, suite='command = ""')
    with pytest.raises(IntentError):
        tools.start_campaign()
    _write_config(project)
    with pytest.raises(ConfigError, match="app.platform"):
        tools.start_campaign(platform="android")  # type: ignore[arg-type]
    assert not (project / "reports").exists() or not any((project / "reports").iterdir())


def test_start_campaign_overrides_intents_and_app(project: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(project)
    make_app(project, "Other.app")
    flow = project / "flow.json"
    flow.write_text(json.dumps(_flow("save")))
    monkeypatch.setattr(campaigns, "launch_runner", lambda campaign, cwd: 1)
    handle = tools.start_campaign(app="Other.app", intents=["flow.json"])
    meta = campaigns.read_meta(Path(handle.report_dir))
    assert meta["shards"] == 2
    config = campaigns.load_request_config(campaigns.CampaignRequest.from_meta(meta))
    assert config.app.path == str(project / "Other.app") and config.intents == [str(flow)]


# cancel_campaign


def test_cancel_requires_mcp_started_campaign(project: Path):
    _campaign(project, "20260101T000000Z-aaaaaa", state="running")
    with pytest.raises(AQAError, match="not started by the MCP server"):
        tools.cancel_campaign("20260101T000000Z-aaaaaa")


def test_cancel_finished_campaign_is_a_no_op(project: Path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path = _campaign(project, "20260101T000000Z-aaaaaa", meta={"pid": dead.pid})
    assert tools.cancel_campaign(path.name).state == "finished"
    assert not (path / campaigns.CANCEL_REQUEST).exists()


def test_cancelled_clock_stops_scheduling(tmp_path: Path):
    clock = _runner.cancellable_clock(tmp_path)()
    assert clock.can_schedule(0).allowed
    campaigns.request_cancel(tmp_path, drain=True)
    decision = clock.can_schedule(0)
    assert not decision.allowed and decision.stop_reason == "cancelled"


def test_hard_cancel_kills_runner_and_marks_stopped(project: Path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    campaigns._children[sleeper.pid] = sleeper
    path = project / "reports" / "20260101T000000Z-aaaaaa"
    ensure_campaign_layout(path)
    write_status(
        CampaignStatus(campaign_id=path.name, state="running", backend="local", workers_configured=1,
                       active_workers=[ActiveWorker("w1", "s-2-flow", "flow", "scripted", "tap")],
                       pending=["s-3-more"]),
        path,
    )
    campaigns.write_meta(path, {"campaign_id": path.name, "pid": sleeper.pid})
    status = tools.cancel_campaign(path.name, drain=False)
    assert sleeper.poll() is not None
    assert status.state == "stopped" and status.cancelled == ["s-2-flow"] and status.pending == ["s-3-more"]
    assert load_status(path).state == "stopped"
    assert json.loads((path / campaigns.CANCEL_REQUEST).read_text())["drain"] is False


# the detached runner, for real


def test_detached_campaign_runs_to_completion(project: Path):
    _write_config(project)
    handle = tools.start_campaign()
    status = _wait_done(handle.campaign_id)
    campaign = Path(handle.report_dir)
    log = (campaign / campaigns.RUNNER_LOG).read_text(encoding="utf-8")
    assert status.state == "finished", log
    assert status.report_dir == str(campaign)
    assert status.failed == ["s-1-suite"] or status.failed, log
    assert (campaign / "summary.md").is_file()
    assert (campaign / "findings.json").is_file()
    result = json.loads((campaign / campaigns.RUNNER_RESULT).read_text())
    assert result["exit_code"] == 1
    # The orchestrator used the pre-chosen id: no second campaign directory.
    assert [p.name for p in campaigns.campaign_dirs()] == [handle.campaign_id]
    items = tools.list_findings(handle.campaign_id)
    assert items and all(item.campaign_id == handle.campaign_id for item in items)
    assert tools.get_finding(items[0].id).id == items[0].id


def test_detached_campaign_drains_on_cancel(project: Path):
    # One worker, the slow suite shard first, then three flows that must never start.
    slow = f"{sys.executable} -c 'import time; time.sleep(3)'"
    flow_dir = project / "intents"
    flow_dir.mkdir()
    for name in ("one", "two", "three"):
        (flow_dir / f"{name}.json").write_text(json.dumps(_flow(name)))
    _write_config(project, campaign='workers = 1\nshard_strategy = "suite"', suite=f'command = "{slow}"')
    config = project / "aqa.config.toml"
    config.write_text('intents = ["intents"]\n' + config.read_text(), encoding="utf-8")
    handle = tools.start_campaign()
    campaign = Path(handle.report_dir)
    deadline = time.monotonic() + 30
    while True:
        status = load_status(campaign)
        if status is not None and status.active_workers:
            break
        assert time.monotonic() < deadline, (campaign / campaigns.RUNNER_LOG).read_text(encoding="utf-8")
        time.sleep(0.05)
    assert tools.cancel_campaign(handle.campaign_id, drain=True).state == "running"
    status = _wait_done(handle.campaign_id)
    log = (campaign / campaigns.RUNNER_LOG).read_text(encoding="utf-8")
    assert status.state == "stopped", log
    assert len(status.completed) == 1 and len(status.pending) == 3, status
    summary = json.loads((campaign / "summary.json").read_text())
    assert "cancelled" in json.dumps(summary), summary


def _flow(name: str) -> dict:
    return {"version": 1, "name": name, "steps": [{"action": "click", "target": {"role": "button", "label": "Save"}}]}


def test_a_runner_that_fails_to_start_is_not_running_forever(project: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(project)

    def broken(campaign, cwd):
        raise OSError("no python")

    monkeypatch.setattr(campaigns, "launch_runner", broken)
    with pytest.raises(AQAError, match="could not start campaign"):
        tools.start_campaign()
    campaign = next((project / "reports").iterdir())
    result = json.loads((campaign / campaigns.RUNNER_RESULT).read_text())
    assert result["exit_code"] == 2 and "no python" in result["error"]
    try:
        status = tools.campaign_status(campaign.name)
    except AQAError:
        return
    assert status.state != "running"
