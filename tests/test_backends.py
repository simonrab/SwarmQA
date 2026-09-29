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
from swarmqa.devices.capacity import HostResources
from swarmqa.devices.locks import try_lock
from swarmqa.errors import BackendUnavailable, ChunkNotReady
from swarmqa.models import Shard, WorkerResult
from swarmqa.spend import SpendMeter
from swarmqa.testing import make_app, sample_config, scripted_shard


@pytest.fixture(autouse=True)
def _host_locks(tmp_path: Path, monkeypatch):
    """Keep host-wide Tart slot locks out of ~/.aqa/locks."""
    monkeypatch.setenv("AQA_LOCK_DIR", str(tmp_path / "locks"))


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
        if len(recorded) >= 3 and recorded[1] == "ip":
            return CompletedProcess(recorded, 0, "192.168.64.10\n", "")
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
    execs = runner.commands("exec")
    assert execs[0] == ["tart", "exec", "aqa-w1", "true"]
    assert runner.commands("ip") == [["tart", "ip", "aqa-w1"]]
    worker = execs[1]
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
        if len(args) >= 2 and args[1] == "ip":
            return CompletedProcess(list(args), 0, "192.168.64.10", "")
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


# WP-A4 regressions: readiness, result.json, campaign dir, symlinks, VM cap.


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SlowBootRunner(FakeRunner):
    """`tart ip` fails `ip_failures` times, then `tart exec <vm> true` fails `exec_failures` times."""

    def __init__(self, *, ip_failures: int = 0, exec_failures: int = 0, **kwargs):
        super().__init__(**kwargs)
        self.ip_failures = ip_failures
        self.exec_failures = exec_failures

    def __call__(self, args, **kwargs):
        recorded = list(args)
        if recorded[1:2] == ["ip"] and self.ip_failures > 0:
            self.ip_failures -= 1
            self.calls.append(recorded)
            return CompletedProcess(recorded, 1, "", "no IP address found")
        if recorded[1:2] == ["exec"] and recorded[-1] == "true" and self.exec_failures > 0:
            self.exec_failures -= 1
            self.calls.append(recorded)
            return CompletedProcess(recorded, 1, "", "guest agent is not running")
        return super().__call__(args, **kwargs)


class NoResultRunner(FakeRunner):
    """The guest writes `write` as result.json, or nothing when it is None."""

    def __init__(self, write: bytes | None = None, **kwargs):
        super().__init__(**kwargs)
        self.write = write

    def _write_guest(self, host_share, worker_id):
        if self.write is None or not host_share:
            return
        path = Path(host_share) / "workers" / worker_id / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.write)


def _vm_config():
    config = sample_config()
    config.vm.image = "base"
    config.vm.recycle = True
    return config


_HUGE = HostResources(memory_bytes=512 * 1024**3, cpu_cores=64)


def test_run_shard_waits_for_ip_then_guest_agent(tmp_path: Path):
    clock = FakeClock()
    runner = SlowBootRunner(ip_failures=2, exec_failures=3)
    backend = TartBackend(_vm_config(), runner=runner, poll_s=1.0, sleep=clock.sleep, clock=clock)

    result = backend.run_shard(scripted_shard(), "w1", campaign_dir=tmp_path / "camp")

    assert result.status == "passed"
    probes = [call for call in runner.calls if call[1] in {"ip", "exec"} and "swarmqa.worker" not in call]
    assert probes == [["tart", "ip", "aqa-w1"]] * 3 + [["tart", "exec", "aqa-w1", "true"]] * 4
    worker_at = next(i for i, call in enumerate(runner.calls) if "swarmqa.worker" in call)
    last_probe = max(i for i, call in enumerate(runner.calls) if call[-1] == "true")
    assert last_probe < worker_at


def test_run_shard_boot_timeout_is_an_error_and_stops_the_vm(tmp_path: Path):
    clock = FakeClock()
    runner = SlowBootRunner(exec_failures=10_000)
    backend = TartBackend(
        _vm_config(), runner=runner, boot_timeout_s=30, poll_s=5, sleep=clock.sleep, clock=clock
    )

    result = backend.run_shard(scripted_shard(), "w1", campaign_dir=tmp_path / "camp")

    assert result.status == "error"
    assert "not ready after 30s" in (result.error or "")
    assert "guest agent" in (result.error or "")
    assert not any("swarmqa.worker" in call for call in runner.calls)
    assert runner.commands("stop") == [["tart", "stop", "aqa-w1"]]
    assert clock.now >= 30


def test_missing_result_json_is_an_error_not_a_pass(tmp_path: Path):
    result = TartBackend(_vm_config(), runner=NoResultRunner()).run_shard(
        scripted_shard(), "w1", campaign_dir=tmp_path / "camp"
    )
    assert result.status == "error"
    assert "no result" in (result.error or "")


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\xfa"])
def test_unreadable_result_json_is_an_error(tmp_path: Path, content: bytes):
    result = TartBackend(_vm_config(), runner=NoResultRunner(write=content)).run_shard(
        scripted_shard(), "w1", campaign_dir=tmp_path / "camp"
    )
    assert result.status == "error"
    assert "could not read" in (result.error or "")


def test_run_shard_uses_the_running_campaign_directory(tmp_path: Path):
    config = _vm_config()
    config.report_root = str(tmp_path / "reports")
    old = tmp_path / "reports" / "20260101-old"
    live = tmp_path / "reports" / "20260102-live"
    for campaign, state in ((old, "completed"), (live, "running")):
        (campaign / "workers" / "w1").mkdir(parents=True)
        (campaign / "status.json").write_text(json.dumps({"state": state}), encoding="utf-8")

    result = TartBackend(config, runner=FakeRunner()).run_shard(scripted_shard(), "w1")

    assert result.status == "passed"
    assert (live / "workers" / "w1" / "result.json").is_file()
    assert (live / "media" / "clip.txt").is_file()
    assert (live / "raw" / "tart" / "w1" / "shard.json").is_file()
    assert not (old / "workers" / "w1" / "result.json").exists()
    assert not (tmp_path / "reports" / "vm").exists()


def test_constructor_campaign_dir_is_used(tmp_path: Path):
    campaign = tmp_path / "given"
    backend = TartBackend(_vm_config(), runner=FakeRunner(), campaign_dir=campaign)
    assert backend.run_shard(scripted_shard(), "w1").status == "passed"
    assert (campaign / "workers" / "w1" / "result.json").is_file()


def test_app_bundle_symlinks_are_copied_as_symlinks(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.vm.image = "base"
    framework = Path(config.app.path) / "Contents" / "Frameworks" / "Kit.framework"
    (framework / "Versions" / "A").mkdir(parents=True)
    (framework / "Versions" / "A" / "Kit").write_text("binary", encoding="utf-8")
    (framework / "Versions" / "Current").symlink_to("A")
    (framework / "Kit").symlink_to("Versions/Current/Kit")
    campaign = tmp_path / "camp"

    TartBackend(config, runner=FakeRunner()).run_shard(scripted_shard(), "w1", campaign_dir=campaign)

    copied = campaign / "raw" / "tart" / "w1" / "app" / "Sample.app" / "Contents" / "Frameworks" / "Kit.framework"
    assert (copied / "Versions" / "Current").is_symlink()
    assert (copied / "Kit").is_symlink()
    assert (copied / "Kit").read_text(encoding="utf-8") == "binary"


def test_pulled_artifacts_keep_symlinks(tmp_path: Path):
    source = tmp_path / "src"
    (source / "w1").mkdir(parents=True)
    (source / "w1" / "a.txt").write_text("a", encoding="utf-8")
    (source / "w1" / "latest").symlink_to("a.txt")
    tart_mod._merge_tree(source, tmp_path / "dest")
    assert (tmp_path / "dest" / "w1" / "latest").is_symlink()


def test_vm_capacity_never_exceeds_two():
    assert TartBackend(_vm_config(), resources=_HUGE).vm_capacity() == 2
    assert TartBackend(_vm_config(), resources=_HUGE, max_vms=5).vm_capacity() == 2
    assert TartBackend(_vm_config(), resources=_HUGE, max_vms=1).vm_capacity() == 1


def test_third_vm_waits_for_a_host_slot_then_errors(tmp_path: Path):
    held = [try_lock(f"{tart_mod.TART_SLOT_PREFIX}-{index}") for index in range(2)]
    assert all(held)
    clock = FakeClock()
    runner = FakeRunner()
    backend = TartBackend(
        _vm_config(), runner=runner, resources=_HUGE, slot_timeout_s=10, poll_s=1,
        sleep=clock.sleep, clock=clock,
    )

    result = backend.run_shard(scripted_shard(), "w3", campaign_dir=tmp_path / "camp")

    assert result.status == "error"
    assert "at most 2 macOS VMs" in (result.error or "")
    assert runner.calls == []
    assert clock.now >= 10
    held[0].release()
    assert backend.run_shard(scripted_shard(), "w3", campaign_dir=tmp_path / "camp").status == "passed"
    held[1].release()


def test_slot_is_released_after_each_shard(tmp_path: Path):
    backend = TartBackend(
        _vm_config(), runner=FakeRunner(), resources=_HUGE, max_vms=1, slot_timeout_s=0
    )
    for worker in ("w1", "w2", "w3"):
        assert backend.run_shard(scripted_shard(), worker, campaign_dir=tmp_path / "c").status == "passed"


def test_create_backend_passes_the_campaign_dir_to_tart(tmp_path):
    from swarmqa.backends import create_backend
    from swarmqa.testing import sample_config

    config = sample_config()
    config.backend = "vm"
    backend = create_backend(config, campaign_dir=tmp_path / "campaign")
    assert backend.campaign_dir == tmp_path / "campaign"


def test_slot_stays_held_when_the_vm_will_not_stop(tmp_path: Path):
    backend = TartBackend(
        _vm_config(), runner=FakeRunner(fail_on="stop"), resources=_HUGE, max_vms=1, slot_timeout_s=0
    )
    first = backend.run_shard(scripted_shard(), "w1", campaign_dir=tmp_path / "c")
    assert first.status == "error"
    second = backend.run_shard(scripted_shard(), "w2", campaign_dir=tmp_path / "c")
    assert second.status == "error"
    assert "at most 1 macOS VMs" in (second.error or "")


def test_default_runner_enforces_timeouts():
    import subprocess
    import sys

    with pytest.raises(subprocess.TimeoutExpired):
        tart_mod._default_runner([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
