"""C9 — Tart VM backend and the cloud spend simulator."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from swarmqa.backends import cloud as cloud_mod
from swarmqa.backends import tart as tart_mod
from swarmqa.backends.cloud import (
    SimulatedCloudBackend,
    create_cloud_backend,
    plan_affordable,
    register_cloud_adapter,
)
from swarmqa.backends.tart import GUEST_SHARE, TartBackend
from swarmqa.errors import BackendUnavailable, ChunkNotReady
from swarmqa.models import Shard, WorkerResult
from swarmqa.spend import SpendMeter
from swarmqa.testing import make_app, sample_config, scripted_shard


def _shard(shard_id: str, name: str) -> Shard:
    shard = scripted_shard()
    shard.id = shard_id
    shard.name = name
    return shard


class FakeRunner:
    def __init__(self, *, vms: list[str] | None = None, fail_on: str | None = None, list_text: str | None = None):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.vms = set(vms or [])
        self.fail_on = fail_on
        self.list_text = list_text

    def __call__(self, args, **kwargs):
        recorded = list(args)
        self.calls.append(recorded)
        self.kwargs.append(kwargs)
        if self.fail_on and self.fail_on in recorded:
            return CompletedProcess(recorded, 1, "", "injected failure")
        if len(recorded) >= 2 and recorded[1] == "list":
            if self.list_text is not None:
                return CompletedProcess(recorded, 0, self.list_text, "")
            payload = [{"Name": name} for name in sorted(self.vms)]
            return CompletedProcess(recorded, 0, json.dumps(payload), "")
        if "swarmqa.worker" in recorded:
            self._write_guest(kwargs.get("host_share"), kwargs.get("worker_id"))
        if len(recorded) >= 2 and recorded[1] == "clone":
            self.vms.add(recorded[-1])
        return CompletedProcess(recorded, 0, "", "")

    def _write_guest(self, host_share: str | None, worker_id: str | None) -> None:
        if not host_share or not worker_id:
            return
        share = Path(host_share)
        assert (share / "shard.json").is_file()
        assert (share / "config.json").is_file()
        assert (share / "worker_entry" / "swarmqa" / "worker.py").is_file()
        worker_dir = share / "workers" / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        result = {
            "worker_id": worker_id,
            "shard_id": "from-guest",
            "status": "passed",
            "backend": "vm",
        }
        (worker_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        media = share / "media"
        media.mkdir(parents=True, exist_ok=True)
        (media / "clip.txt").write_text("frame", encoding="utf-8")

    def commands(self, name: str) -> list[list[str]]:
        return [call for call in self.calls if len(call) >= 2 and call[1] == name]


def test_ensure_available_rejects_non_darwin():
    backend = TartBackend(sample_config())
    with pytest.raises(BackendUnavailable) as exc:
        backend.ensure_available()
    message = str(exc.value)
    assert "backend = local" in message
    assert "docs/backends.md" in message


def test_ensure_available_rejects_intel_mac(monkeypatch):
    monkeypatch.setattr(tart_mod.sys, "platform", "darwin")
    monkeypatch.setattr(tart_mod.platform, "machine", lambda: "x86_64")
    backend = TartBackend(sample_config())
    with pytest.raises(BackendUnavailable) as exc:
        backend.ensure_available()
    assert "backend = local" in str(exc.value)
    assert "docs/backends.md" in str(exc.value)


def test_ensure_available_rejects_missing_tart(monkeypatch):
    monkeypatch.setattr(tart_mod.sys, "platform", "darwin")
    monkeypatch.setattr(tart_mod.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(tart_mod.shutil, "which", lambda binary: None)
    backend = TartBackend(sample_config())
    with pytest.raises(BackendUnavailable) as exc:
        backend.ensure_available()
    message = str(exc.value)
    assert "backend = local" in message
    assert "docs/backends.md" in message
    assert "tart" in message


def test_ensure_available_accepts_apple_silicon(monkeypatch, tmp_path: Path):
    binary = tmp_path / "tart"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(tart_mod.sys, "platform", "darwin")
    monkeypatch.setattr(tart_mod.platform, "machine", lambda: "arm64")
    config = sample_config()
    config.vm.tart_bin = str(binary)
    TartBackend(config).ensure_available()


def test_vm_cost_rate_comes_from_config():
    config = sample_config()
    config.vm.cost_per_worker_minute = 0.02
    assert TartBackend(config, runner=lambda *args, **kwargs: None).cost_per_worker_minute() == 0.02


def test_run_shard_clones_copies_runs_and_pulls(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.vm.image = "ghcr.io/cirruslabs/macos-sonoma-base:latest"
    config.vm.recycle = True
    runner = FakeRunner()
    backend = TartBackend(config, runner=runner)
    campaign = tmp_path / "campaign"
    shard = scripted_shard()

    result = backend.run_shard(shard, "w1", campaign_dir=campaign)

    assert result.status == "passed"
    assert result.backend == "vm"
    assert result.worker_id == "w1"
    assert result.shard_id == "from-guest"
    assert runner.commands("clone") == [
        ["tart", "clone", config.vm.image, "aqa-w1"],
    ]
    run = runner.commands("run")[0]
    assert run[0] == "tart"
    assert run[-1] == "aqa-w1"
    assert run[2].startswith("--dir=swarmqa:")
    worker = runner.commands("exec")[0]
    assert worker[2] == "aqa-w1"
    assert "python" in worker
    assert "-m" in worker
    assert "swarmqa.worker" in worker
    assert GUEST_SHARE in worker
    assert f"{GUEST_SHARE}/shard.json" in worker
    assert "--worker-id" in worker
    assert worker[-1] == "w1"
    assert runner.commands("stop") == [["tart", "stop", "aqa-w1"]]
    assert runner.commands("delete") == []
    assert (campaign / "workers" / "w1" / "result.json").is_file()
    assert (campaign / "media" / "clip.txt").read_text(encoding="utf-8") == "frame"
    share = campaign / "raw" / "tart" / "w1"
    assert (share / "worker_entry" / "swarmqa" / "worker.py").is_file()
    assert (share / "app" / "Sample.app").is_dir()
    guest_config = json.loads((share / "config.json").read_text(encoding="utf-8"))
    assert guest_config["app"]["path"] == f"{GUEST_SHARE}/app/Sample.app"


def test_run_shard_reuses_listed_vm(tmp_path: Path):
    config = sample_config()
    config.vm.image = "base"
    config.vm.recycle = True
    runner = FakeRunner(list_text="Source Name Disk State\nlocal aqa-w2 50 stopped\n")
    backend = TartBackend(config, runner=runner)

    result = backend.run_shard(scripted_shard(), "w2", campaign_dir=tmp_path / "camp")

    assert result.status == "passed"
    assert runner.commands("clone") == []
    assert runner.commands("run")[0][-1] == "aqa-w2"
    assert runner.commands("stop") == [["tart", "stop", "aqa-w2"]]


def test_recycle_false_deletes_the_vm(tmp_path: Path):
    config = sample_config()
    config.vm.image = "base"
    config.vm.recycle = False
    runner = FakeRunner()
    TartBackend(config, runner=runner).run_shard(scripted_shard(), "w3", campaign_dir=tmp_path / "camp")
    assert runner.commands("stop") == [["tart", "stop", "aqa-w3"]]
    assert runner.commands("delete") == [["tart", "delete", "aqa-w3"]]


def test_runner_failure_does_not_stop_a_sibling(tmp_path: Path):
    config = sample_config()
    config.vm.image = "base"
    config.vm.recycle = True
    failing = FakeRunner(fail_on="swarmqa.worker")
    backend = TartBackend(config, runner=failing)
    campaign = tmp_path / "camp"

    first = backend.run_shard(scripted_shard(), "w1", campaign_dir=campaign)

    assert first.status == "error"
    assert "injected failure" in (first.error or "")
    assert failing.commands("stop") == [["tart", "stop", "aqa-w1"]]
    assert all(call[-1] != "aqa-w2" for call in failing.calls)

    healthy = FakeRunner()
    backend.runner = healthy
    second = backend.run_shard(_shard("s-2", "other"), "w2", campaign_dir=campaign)

    assert second.status == "passed"
    assert healthy.commands("clone") == [["tart", "clone", "base", "aqa-w2"]]
    assert healthy.commands("stop") == [["tart", "stop", "aqa-w2"]]
    assert all(call[-1] != "aqa-w1" for call in healthy.calls)


def test_missing_image_fails_only_that_shard(tmp_path: Path):
    config = sample_config()
    config.vm.image = None
    runner = FakeRunner()
    result = TartBackend(config, runner=runner).run_shard(
        scripted_shard(),
        "w1",
        campaign_dir=tmp_path / "camp",
    )
    assert result.status == "error"
    assert "vm.image" in (result.error or "")
    assert runner.commands("clone") == []
    assert runner.commands("stop") == []


def test_cancel_stops_only_the_named_vm_when_policy_is_cancel():
    config = sample_config()
    config.spend.overrun = "cancel"
    runner = FakeRunner()
    TartBackend(config, runner=runner).cancel("w9")
    assert runner.calls == [["tart", "stop", "aqa-w9"]]


def test_cancel_drain_does_not_stop_the_vm():
    config = sample_config()
    assert config.spend.overrun == "drain"
    assert config.budgets.on_budget == "drain"
    runner = FakeRunner()
    TartBackend(config, runner=runner).cancel("w9")
    assert runner.calls == []


def test_run_shard_without_runner_checks_the_host(tmp_path: Path):
    config = sample_config()
    config.vm.image = "base"
    with pytest.raises(BackendUnavailable) as exc:
        TartBackend(config).run_shard(scripted_shard(), "w1", campaign_dir=tmp_path / "camp")
    assert "backend = local" in str(exc.value)


def test_simulator_bills_measured_minutes():
    config = sample_config()
    config.cloud.cost_per_worker_minute = 0.05

    def executor(shard: Shard, worker_id: str) -> WorkerResult:
        return WorkerResult(
            worker_id=worker_id,
            shard_id=shard.id,
            status="failed",
            worker_minutes=2.0,
            backend="local",
            shard_name=shard.name,
            shard_kind=shard.kind,
        )

    result = SimulatedCloudBackend(config, executor=executor).run_shard(scripted_shard(), "c1")
    assert result.status == "failed"
    assert result.backend == "cloud"
    assert result.worker_minutes >= 2.0
    assert result.estimated_cost == pytest.approx(0.05 * result.worker_minutes)


def test_simulator_executor_failure_is_one_shard():
    config = sample_config()
    calls: list[str] = []

    def executor(shard: Shard, worker_id: str) -> WorkerResult:
        calls.append(worker_id)
        if worker_id == "c1":
            raise RuntimeError("guest lost the session")
        return WorkerResult(worker_id=worker_id, shard_id=shard.id, status="passed")

    backend = SimulatedCloudBackend(config, executor=executor)
    first = backend.run_shard(scripted_shard(), "c1")
    second = backend.run_shard(_shard("s-2", "next"), "c2")
    assert first.status == "error"
    assert "guest lost" in (first.error or "")
    assert second.status == "passed"
    assert calls == ["c1", "c2"]
    assert second.estimated_cost == pytest.approx(
        config.cloud.cost_per_worker_minute * second.worker_minutes
    )


def test_simulator_without_executor_uses_local_backend():
    shard = scripted_shard()
    result = SimulatedCloudBackend(sample_config()).run_shard(shard, "c1")
    assert result.backend == "cloud"
    assert result.shard_id == shard.id
    assert result.status == "failed"
    assert result.estimated_cost >= 0


def test_create_cloud_backend_returns_the_simulator():
    config = sample_config()
    for adapter in ("simulator", "sim", ""):
        config.cloud.adapter = adapter
        backend = create_cloud_backend(config)
        assert isinstance(backend, SimulatedCloudBackend)
        assert backend.cost_per_worker_minute() == config.cloud.cost_per_worker_minute


def test_paid_mac_adapter_is_selected_by_name():
    config = sample_config()
    config.cloud.adapter = "paid-mac"

    class Paid:
        name = "cloud"

        def __init__(self, cfg):
            self.config = cfg

        def cost_per_worker_minute(self) -> float:
            return 1.5

        def run_shard(self, shard, worker_id):
            raise AssertionError("not called")

        def cancel(self, worker_id):
            return None

    register_cloud_adapter("paid-mac", Paid)
    try:
        backend = create_cloud_backend(config)
        assert isinstance(backend, Paid)
        assert backend.cost_per_worker_minute() == 1.5

        config.cloud.adapter = "missing-host"
        with pytest.raises(ChunkNotReady) as exc:
            create_cloud_backend(config)
        assert "RunnerBackend" in str(exc.value)
        assert "missing-host" in str(exc.value)
    finally:
        cloud_mod._ADAPTERS.pop("paid-mac", None)


def test_plan_affordable_cap_allows_only_the_first_shard():
    shards = [_shard("s-1", "first"), _shard("s-2", "second")]
    meter = SpendMeter(1.0, "USD")
    runnable, reason = plan_affordable(shards, meter, rate=1.0, minutes=1.0)
    assert runnable == [shards[0]]
    assert runnable[0] is shards[0]
    assert reason == "spend_cap"
    assert meter.spent == pytest.approx(1.0)
    assert meter.stop_reason == "spend_cap"


def test_plan_affordable_none_cap_allows_both_shards():
    shards = [_shard("s-1", "first"), _shard("s-2", "second")]
    meter = SpendMeter(None, "USD")
    runnable, reason = plan_affordable(shards, meter, rate=9.0, minutes=3.0)
    assert runnable == shards
    assert reason is None
    assert meter.stop_reason is None


def test_plan_affordable_refuses_a_shard_that_starts_over_cap():
    shards = [_shard("s-1", "first"), _shard("s-2", "second")]
    meter = SpendMeter(0.5, "USD")
    runnable, reason = plan_affordable(shards, meter, rate=1.0, minutes=1.0)
    assert runnable == []
    assert reason == "spend_cap"
    assert meter.spent == 0


def test_cloud_cancel_follows_overrun_policy():
    config = sample_config()
    config.spend.overrun = "drain"
    backend = SimulatedCloudBackend(config, executor=lambda shard, worker_id: WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        status="passed",
    ))
    backend.cancel("c1")
    assert backend.cancelled == set()

    config.spend.overrun = "cancel"
    backend.cancel("c1")
    backend.cancel("c2")
    assert backend.cancelled == {"c1", "c2"}


def test_budget_cancel_stops_the_tart_vm():
    config = sample_config()
    config.budgets.on_budget = "cancel"
    runner = FakeRunner()
    TartBackend(config, runner=runner).cancel("w4")
    assert runner.calls == [["tart", "stop", "aqa-w4"]]


def test_runner_exception_fails_only_that_shard(tmp_path: Path):
    config = sample_config()
    config.vm.image = "base"

    def runner(args, **kwargs):
        if "swarmqa.worker" in args:
            raise RuntimeError("guest agent closed")
        if len(args) >= 2 and args[1] == "list":
            return CompletedProcess(list(args), 0, "[]", "")
        return CompletedProcess(list(args), 0, "", "")

    result = TartBackend(config, runner=runner).run_shard(
        scripted_shard(),
        "w1",
        campaign_dir=tmp_path / "camp",
    )
    assert result.status == "error"
    assert "guest agent closed" in (result.error or "")
