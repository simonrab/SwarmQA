from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from swarmqa.backends.local import LocalBackend
from swarmqa.driver.fake import FakeDriver
from swarmqa.models import Finding, RunOptions, Shard, WorkerResult
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.orchestrator.status import model_from_plain, read_status
from swarmqa.serialize import dump_json, load_json
from swarmqa.testing import make_app, sample_config, sample_tree, scripted_shard
from swarmqa.worker import main


def _config(tmp_path: Path, **updates):
    config = sample_config()
    config.report_root = str(tmp_path / "reports")
    for name, value in updates.items():
        setattr(config, name, value)
    return config


def _shard(shard_id: str, kind: str = "scripted", **kwargs) -> Shard:
    return Shard(id=shard_id, kind=kind, name=kwargs.pop("name", shard_id), **kwargs)


def _passed(shard: Shard, worker_id: str, **kwargs) -> WorkerResult:
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        shard_name=shard.name,
        shard_kind=shard.kind,
        status="passed",
        **kwargs,
    )


def test_two_shards_overlap_under_worker_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config = _config(tmp_path)
    config.workers = 2
    shards = [_shard(f"s-{index}", name=f"shard-{index}") for index in range(3)]
    lock = threading.Lock()
    release = threading.Event()
    state = {"current": 0, "max": 0, "overlapped": False, "seen_active": []}

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
            if state["current"] >= 2 and not state["seen_active"]:
                state["overlapped"] = True
                status_files = list(Path(config.report_root).glob("*/status.json"))
                assert status_files
                payload = json.loads(status_files[0].read_text(encoding="utf-8"))
                state["seen_active"].append(len(payload["active_workers"]))
                release.set()
        assert release.wait(timeout=5)
        with lock:
            state["current"] -= 1
        return _passed(shard, worker_id)

    result = run_campaign(config, shards, executor=executor)
    assert state["overlapped"] is True
    assert state["max"] == 2
    assert max(state["seen_active"]) >= 2
    assert [item.status for item in result.results] == ["passed", "passed", "passed"]
    assert result.coverage.completed == 3
    assert result.coverage.not_started == 0
    assert result.coverage.stop_reason is None
    report = Path(result.report_dir)
    assert report.parent == Path(config.report_root)
    assert (report / "summary.md").is_file()
    assert (report / "summary.json").is_file()
    assert len(list(report.glob("workers/*/result.json"))) == 3
    assert len([path for path in Path(config.report_root).iterdir() if path.is_dir()]) == 1
    err = capsys.readouterr().err
    assert f"[campaign {result.campaign_id}]" in err
    assert "backend=local" in err
    assert "mode=scripted" in err
    assert "queue=" in err and "active=" in err and "spend=" in err


def test_merged_report_dedups_findings(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 2
    shards = [_shard("s-a", name="alpha"), _shard("s-b", name="beta")]
    fingerprint = "abc123abc123abcd"

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        return WorkerResult(
            worker_id=worker_id,
            shard_id=shard.id,
            shard_name=shard.name,
            shard_kind=shard.kind,
            status="failed",
            findings=[
                Finding(
                    id=f"f-{worker_id}",
                    title="Save did nothing",
                    severity="high",
                    kind="assertion",
                    steps=["click Save"],
                    fingerprint=fingerprint,
                    worker_id=worker_id,
                    backend="local",
                    shard_id=shard.id,
                    screenshots=[f"media/{worker_id}.png"],
                )
            ],
        )

    result = run_campaign(config, shards, executor=executor)
    report = Path(result.report_dir)
    text = (report / "summary.md").read_text(encoding="utf-8")
    assert text.count("Save did nothing") == 1
    assert "w1" in text and "w2" in text
    payload = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    findings = [finding for item in payload["results"] for finding in item["findings"]]
    assert len(findings) == 1
    assert findings[0]["worker_ids"] == ["w1", "w2"]
    assert findings[0]["screenshots"] == ["media/w1.png", "media/w2.png"]
    assert result.exit_code == 1
    assert result.coverage.failed == 2


def test_worker_exception_does_not_abort_sibling(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 2
    shards = [_shard("s-bad", name="bad"), _shard("s-good", name="good")]

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        if shard.id == "s-bad":
            raise RuntimeError("worker blew up")
        return _passed(shard, worker_id)

    result = run_campaign(config, shards, executor=executor)
    by_id = {item.shard_id: item for item in result.results}
    assert set(by_id) == {"s-bad", "s-good"}
    assert by_id["s-bad"].status == "error"
    assert "worker blew up" in (by_id["s-bad"].error or "")
    assert by_id["s-good"].status == "passed"
    assert (Path(result.report_dir) / "summary.md").is_file()
    assert result.exit_code == 1
    assert result.coverage.failed == 1
    assert result.coverage.completed == 1


def test_budget_stop_leaves_not_started(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 1
    config.budgets.max_worker_minutes = 1
    shards = [_shard(f"s-{index}") for index in range(3)]

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        return _passed(shard, worker_id, worker_minutes=1)

    result = run_campaign(config, shards, executor=executor)
    assert result.coverage.stop_reason == "budget"
    assert result.coverage.completed == 1
    assert result.coverage.not_started == 2
    assert [item.shard_id for item in result.results] == ["s-0"]
    status = json.loads((Path(result.report_dir) / "status.json").read_text(encoding="utf-8"))
    assert status["pending"] == ["s-1", "s-2"]
    assert status["state"] == "stopped"
    assert status["queue_depth"] == 2
    text = read_status(Path(config.report_root), result.campaign_id)
    assert f"[campaign {result.campaign_id}] queue=2 active=0 backend=local" in text


def test_local_max_spend_is_ignored(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 2
    config.spend.max_spend = 0.01
    shards = [_shard("s-1", name="one"), _shard("s-2", name="two")]

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        return _passed(shard, worker_id)

    result = run_campaign(config, shards, executor=executor)
    assert LocalBackend(config).cost_per_worker_minute() == 0
    assert result.spend.note == "max_spend is ignored on the local backend"
    assert result.spend.stop_reason is None
    assert result.coverage.stop_reason is None
    assert result.coverage.completed == 2
    assert result.coverage.not_started == 0
    summary = (Path(result.report_dir) / "summary.md").read_text(encoding="utf-8")
    assert "max_spend is ignored on the local backend" in summary


def test_spend_cap_stops_metered_backend(tmp_path: Path):
    config = _config(tmp_path)
    config.backend = "cloud"
    config.workers = 2
    config.spend.max_spend = 1
    config.cloud.cost_per_worker_minute = 1
    config.cloud.estimated_shard_minutes = 1
    shards = [_shard("s-1"), _shard("s-2")]
    result = run_campaign(config, shards)
    assert result.coverage.stop_reason == "spend_cap"
    assert result.spend.stop_reason == "spend_cap"
    assert result.spend.estimated_spent == pytest.approx(1)
    assert result.coverage.not_started == 1
    assert len(result.results) == 1
    assert result.results[0].status == "error"
    assert result.results[0].shard_id == "s-1"


@pytest.mark.parametrize(
    ("kind", "status", "fail_on", "with_finding", "expected"),
    [
        ("scripted", "failed", "scripted", False, 1),
        ("exploratory", "failed", "scripted", True, 0),
        ("exploratory", "failed", "any", True, 1),
        ("scripted", "failed", "never", False, 0),
        ("scripted", "passed", "any", False, 0),
        ("scripted", "error", "scripted", False, 1),
        ("suite", "failed", "scripted", True, 1),
        ("visual", "failed", "scripted", True, 0),
    ],
)
def test_fail_on_exit_code(tmp_path, kind, status, fail_on, with_finding, expected):
    config = _config(tmp_path)
    config.fail_on = fail_on
    config.workers = 1
    shard = _shard("s-1", kind=kind)

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        findings = []
        if with_finding:
            findings.append(
                Finding(
                    id="f1",
                    title="found",
                    severity="medium",
                    kind="assertion" if kind != "visual" else "visual",
                    steps=["step"],
                    fingerprint="ff" * 8,
                    worker_id=worker_id,
                    backend="local",
                    shard_id=shard.id,
                )
            )
        return WorkerResult(
            worker_id=worker_id,
            shard_id=shard.id,
            status=status,
            findings=findings,
            error="boom" if status == "error" else None,
        )

    result = run_campaign(config, [shard], executor=executor)
    assert result.exit_code == expected


def test_fake_driver_scripted_records_chunk_gap(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    seen: list[Path] = []

    def factory(target, work_dir):
        assert target is app
        seen.append(Path(work_dir))
        driver = FakeDriver(target, work_dir)
        driver.set_tree(sample_tree())
        return driver

    result = run_campaign(config, [scripted_shard()], driver_factory=factory)
    assert seen
    assert "workers" in seen[0].parts
    assert result.results[0].status == "error"
    assert "C4 is not implemented" in (result.results[0].error or "")
    assert (Path(result.report_dir) / "summary.md").is_file()
    assert result.exit_code == 1


def test_exploratory_gap_does_not_stop_suite(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 2
    shards = [
        _shard("s-ex", kind="exploratory", goal="Save the document"),
        _shard(
            "s-suite",
            kind="suite",
            suite_command=f"{sys.executable} -c 'print(\"suite-ok\")'",
        ),
    ]
    result = run_campaign(config, shards)
    by_id = {item.shard_id: item for item in result.results}
    assert "C5 is not implemented" in (by_id["s-ex"].error or "")
    assert by_id["s-ex"].status == "error"
    assert by_id["s-suite"].status == "passed"
    logs = list(Path(result.report_dir).glob("raw/*.log"))
    assert any("suite-ok" in path.read_text(encoding="utf-8") for path in logs)
    assert (Path(result.report_dir) / "summary.md").is_file()


def test_visual_chunk_gap_records_error(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    shard = _shard("s-vis", kind="visual", visual_names=["home"])
    result = run_campaign(config, [shard])
    assert result.results[0].status == "error"
    assert "C6 is not implemented" in (result.results[0].error or "")
    assert (Path(result.report_dir) / "summary.md").is_file()


def test_subprocess_isolation_writes_worker_result(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 1
    config.local.isolation = "subprocess"
    shard = _shard(
        "s-suite",
        kind="suite",
        suite_command=f"{sys.executable} -c 'print(\"from-worker\")'",
    )
    result = run_campaign(config, [shard])
    assert result.results[0].status == "passed"
    assert result.exit_code == 0
    raw = "\n".join(path.read_text(encoding="utf-8") for path in Path(result.report_dir).glob("raw/*"))
    assert "from-worker" in raw
    assert (Path(result.report_dir) / "workers" / "w1" / "result.json").is_file()


def test_worker_exit_codes(tmp_path: Path):
    camp = tmp_path / "camp"
    shard = _shard(
        "s-fail",
        kind="suite",
        suite_command=f"{sys.executable} -c 'raise SystemExit(3)'",
    )
    config = sample_config()
    shard_path = tmp_path / "shard.json"
    config_path = tmp_path / "config.json"
    dump_json(shard, shard_path)
    dump_json(config, config_path)
    code = main(
        [
            "--campaign-dir",
            str(camp),
            "--shard-file",
            str(shard_path),
            "--config-json",
            str(config_path),
            "--worker-id",
            "w9",
        ]
    )
    assert code == 0
    payload = json.loads((camp / "workers" / "w9" / "result.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"

    scripted = scripted_shard()
    scripted_path = tmp_path / "scripted.json"
    dump_json(scripted, scripted_path)
    scripted_camp = tmp_path / "scripted-camp"
    code = main(
        [
            "--campaign-dir",
            str(scripted_camp),
            "--shard-file",
            str(scripted_path),
            "--config-json",
            str(config_path),
            "--worker-id",
            "w1",
        ]
    )
    assert code == 0
    payload = json.loads((scripted_camp / "workers" / "w1" / "result.json").read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "C4" in payload["error"]

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    code = main(
        [
            "--campaign-dir",
            str(blocker),
            "--shard-file",
            str(shard_path),
            "--config-json",
            str(config_path),
            "--worker-id",
            "w1",
        ]
    )
    assert code == 1


def test_resume_reruns_failed_only(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 1
    shards = [_shard("s-good", name="good"), _shard("s-bad", name="bad")]
    calls: list[str] = []

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        calls.append(shard.id)
        if shard.id == "s-bad":
            raise RuntimeError("nope")
        return _passed(shard, worker_id)

    first = run_campaign(config, shards, executor=executor)
    assert calls == ["s-good", "s-bad"]
    calls.clear()

    def rerun(shard, worker_id, work_dir, _config):
        del work_dir
        calls.append(shard.id)
        return _passed(shard, worker_id)

    second = run_campaign(
        config,
        shards,
        options=RunOptions(resume_campaign_id=first.campaign_id),
        executor=rerun,
    )
    assert calls == ["s-bad"]
    assert second.campaign_id == first.campaign_id
    assert second.coverage.completed == 2
    assert second.coverage.failed == 0
    by_id = {item.shard_id: item for item in second.results}
    assert by_id["s-good"].worker_id == "w1"
    assert by_id["s-bad"].status == "passed"
    assert len([path for path in Path(config.report_root).iterdir() if path.is_dir()]) == 1


def test_resume_carries_spend_unless_reset(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 1
    shard = _shard("s-1")
    calls: list[str] = []

    def executor(shard, worker_id, work_dir, _config):
        del work_dir
        calls.append(shard.id)
        return _passed(shard, worker_id)

    first = run_campaign(config, [shard], executor=executor)
    assert calls == ["s-1"]
    status_path = Path(first.report_dir) / "status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["spend_estimated"] = 4.25
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    calls.clear()
    carried = run_campaign(
        config,
        [shard],
        options=RunOptions(resume_campaign_id=first.campaign_id),
        executor=executor,
    )
    assert calls == []
    assert carried.spend.estimated_spent == pytest.approx(4.25)
    assert carried.results[0].status == "passed"

    reset = run_campaign(
        config,
        [shard],
        options=RunOptions(resume_campaign_id=first.campaign_id, reset_spend=True),
        executor=executor,
    )
    assert reset.spend.estimated_spent == pytest.approx(0)


def test_gui_warning_mentions_vm_or_cloud(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config = _config(tmp_path)
    config.workers = 3
    run_campaign(config, [_shard("s-1")], executor=lambda shard, worker_id, work_dir, _config: _passed(shard, worker_id))
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "vm" in err and "cloud" in err
    assert "isolated GUI" in err


def test_no_gui_warning_for_suite_only_or_small_fleet(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config = _config(tmp_path)
    config.workers = 4
    shard = _shard("s-suite", kind="suite", suite_command=f"{sys.executable} -c 'print(1)'")
    run_campaign(config, [shard])
    err = capsys.readouterr().err
    assert "isolated GUI" not in err

    config = _config(tmp_path / "small")
    config.workers = 2
    run_campaign(
        config,
        [_shard("s-1")],
        executor=lambda shard, worker_id, work_dir, _config: _passed(shard, worker_id),
    )
    err = capsys.readouterr().err
    assert "isolated GUI" not in err


def test_cancel_inflight_on_budget(tmp_path: Path):
    config = _config(tmp_path)
    config.workers = 1
    config.local.isolation = "subprocess"
    config.budgets.max_wall_time_s = 2
    config.budgets.on_budget = "cancel"
    shards = [
        _shard(
            "s-sleep",
            kind="suite",
            suite_command=f"{sys.executable} -c 'import time; time.sleep(20)'",
        ),
        _shard(
            "s-next",
            kind="suite",
            suite_command=f"{sys.executable} -c 'print(1)'",
        ),
    ]
    started = time.monotonic()
    result = run_campaign(config, shards)
    elapsed = time.monotonic() - started
    assert elapsed < 12
    by_id = {item.shard_id: item for item in result.results}
    assert by_id["s-sleep"].status == "cancelled"
    assert "s-next" not in by_id
    assert result.coverage.cancelled == 1
    assert result.coverage.not_started == 1
    assert result.coverage.stop_reason == "budget"


def test_read_status_includes_active_worker(tmp_path: Path):
    from swarmqa.models import ActiveWorker, CampaignStatus
    from swarmqa.orchestrator.status import write_status

    report = tmp_path / "reports" / "camp-1"
    status = CampaignStatus(
        campaign_id="camp-1",
        state="running",
        backend="local",
        workers_configured=2,
        active_workers=[
            ActiveWorker(worker_id="w1", shard_id="s-1", shard_name="save", mode="scripted", step="click")
        ],
        queue_depth=3,
        spend_estimated=1.5,
        spend_cap=10,
        spend_currency="USD",
        report_dir=str(report),
    )
    write_status(status, report)
    text = read_status(tmp_path / "reports", "camp-1")
    assert text == (
        "[campaign camp-1] queue=3 active=1 backend=local spend=1.5/10 USD\n"
        "[worker w1] shard=save mode=scripted step=click\n"
    )
    assert read_status(tmp_path / "reports").splitlines()[0].startswith("[campaign camp-1]")


def test_model_roundtrip_for_worker_files(tmp_path: Path):
    from swarmqa.models import CampaignConfig, Shard

    shard = scripted_shard()
    config = sample_config()
    config.local.isolation = "subprocess"
    config.spend.max_spend = 3.5
    shard_path = tmp_path / "shard.json"
    config_path = tmp_path / "config.json"
    dump_json(shard, shard_path)
    dump_json(config, config_path)
    loaded_shard = model_from_plain(Shard, load_json(shard_path))
    loaded_config = model_from_plain(CampaignConfig, load_json(config_path))
    assert loaded_shard.actions[0].action == "click"
    assert loaded_shard.actions[0].target is not None
    assert loaded_shard.actions[0].target.label == "Save"
    assert loaded_config.local.isolation == "subprocess"
    assert loaded_config.spend.max_spend == pytest.approx(3.5)


def test_empty_queue_writes_summary(tmp_path: Path):
    config = _config(tmp_path)
    result = run_campaign(config, [])
    assert result.exit_code == 0
    assert result.coverage.stop_reason is None
    assert result.results == []
    assert (Path(result.report_dir) / "summary.json").is_file()
