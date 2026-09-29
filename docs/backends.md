# Runner backends

SwarmQA runs each shard on one backend. `backend = local` uses the host process. `backend = vm` uses Tart on Apple Silicon. `backend = cloud` uses a metered adapter. v1 ships the Tart adapter and a local cost simulator. A paid Mac host plugs in later behind the same interface.

The orchestrator asks `swarmqa.backends.create_backend` for the selected backend. It does not import a cloud vendor.

## Tart on Apple Silicon

Tart runs macOS guests through Apple's Virtualization framework. SwarmQA refuses to start it anywhere else.

`TartBackend.ensure_available()` raises `BackendUnavailable` when:

- the host is not macOS, or the CPU is not ARM (`arm64`)
- the `tart` binary from `vm.tart_bin` (default `tart`) is not on `PATH`, and is not an executable absolute path

The message tells you to set `backend = local` and points here. Importing the module on Linux succeeds. Tests inject `runner(args, **kwargs)` and never need a Tart binary.

### Install

On Apple Silicon, macOS 13 (Ventura) or newer:

```bash
brew install cirruslabs/cli/tart
```

A release tarball is the other install path:

```bash
curl -LO https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz
tar -xzvf tart.tar.gz
./tart.app/Contents/MacOS/tart --version
```

Point `vm.tart_bin` at that executable when it is not on `PATH`. The `.app` binary is the one that picks up Tart's provisioning profile.

### Images

`vm.image` is the clone source. Tart pulls OCI images published by Tart (not Docker images). Apple Silicon macOS images:

| Image | Use |
| --- | --- |
| `ghcr.io/cirruslabs/macos-sonoma-base:latest` | macOS 14 base guest |
| `ghcr.io/cirruslabs/macos-sequoia-base:latest` | macOS 15 base guest |
| `ghcr.io/cirruslabs/macos-tahoe-base:latest` | current macOS base guest |
| `ghcr.io/cirruslabs/macos-sonoma-xcode:latest` | guest with Xcode, for suite shards |

`tart clone` pulls the image when it is not already local. Base images are large (tens of GB). Guests published by Cirrus use `admin` / `admin` and should have Remote Login and the Tart guest agent so `tart exec` can run without SSH.

```toml
[campaign]
backend = "vm"
workers = 2

[vm]
provider = "tart"
image = "ghcr.io/cirruslabs/macos-sonoma-base:latest"
tart_bin = "tart"
recycle = true
cost_per_worker_minute = 0.0
```

The guest needs Python 3.11 or newer. SwarmQA copies its own package into the VM for `python -m swarmqa.worker`, so the image does not have to preinstall SwarmQA.

### What one worker does

`run_shard` owns a single VM named `aqa-<worker-id>`. Results and artifacts go to the campaign directory: the `campaign_dir` argument or constructor keyword when given, otherwise the running campaign under `report_root` that has `workers/<worker-id>`, otherwise `report_root/vm`.

0. Take one of the host-wide Tart slots (`tart-vm-slot-0` or `-1` under `~/.aqa/locks`). Apple's macOS licence allows two macOS VMs per Mac, so a host never runs more than two, across every campaign and `TartVMPool`. A worker waits up to `slot_timeout_s` (default 30 minutes) and then ends `error`.
1. `tart list`. An existing VM with that name is reused. Otherwise `tart clone <vm.image> aqa-<worker-id>`.
2. The host writes the shard JSON, campaign config, a copy of the `swarmqa` package (the worker entry), and the app bundle when `app.path` is a directory. Those files live under the campaign's `raw/tart/<worker-id>/` directory. Copies keep symlinks as symlinks, so `.app` framework bundles stay intact.
3. `tart run --dir=swarmqa:<that directory> aqa-<worker-id>` starts the guest in the background. macOS guests see the share at `/Volumes/My Shared Files/swarmqa`. SwarmQA then polls `tart ip aqa-<worker-id>` until the guest has an address, and `tart exec aqa-<worker-id> true` until the guest agent answers. If that takes longer than `boot_timeout_s` (default 300 s), the shard ends `error` and the VM is stopped.
4. `tart exec` runs `python -m swarmqa.worker` with `--campaign-dir`, `--shard-file`, `--config-json`, and `--worker-id`. `PYTHONPATH` points at the copied package.
5. After the guest exits, SwarmQA copies `workers/` and `media/` from the mounted share back to the campaign directory. The `--dir` mount is how the shard, the worker entry, and the artifacts move between host and guest. A missing or unreadable `workers/<worker-id>/result.json` ends the shard `error`, never `passed`.
6. `vm.recycle = true` (the default) runs `tart stop` and keeps the clone for the next shard. `vm.recycle = false` stops the VM and `tart delete`s it.

A non-zero runner status or a runner exception marks that shard `error` and returns. The slot is released after the VM stops. The method does not stop any other `aqa-*` VM. `cancel(worker_id)` stops only `aqa-<worker-id>`, and only when `spend.overrun` or `budgets.on_budget` is `cancel`.

Device pools, including `TartVMPool`, which leases Tart VMs to runner drivers, are described in `docs/devices.md`.

`cost_per_worker_minute()` returns `vm.cost_per_worker_minute`. The shard result's `estimated_cost` is that rate times the measured minutes.

## Cloud adapter and the spend cap

`cloud.adapter = "simulator"` (also `sim` or empty) selects `SimulatedCloudBackend`. It runs the shard through `executor(shard, worker_id)` when that keyword was passed, and otherwise through `LocalBackend.run_shard` once that method exists. `WorkerResult.estimated_cost` is `cloud.cost_per_worker_minute` times the measured minutes. When the executor reports a larger `worker_minutes`, that value is the billable time.

Scheduling uses a pre-check, not the measured time. `plan_affordable(shards, meter, rate, minutes)` prices every shard at `rate * minutes` and asks `swarmqa.spend.SpendMeter`. Pass `cloud.estimated_shard_minutes` (default 1 minute) as `minutes`. The meter reserves each shard that fits. The first shard that would pass the cap is left unscheduled, along with everything after it, and the stop reason is `spend_cap`. A meter created with `cap is None` allows every shard.

`backend = cloud` requires `spend.max_spend > 0`. The sample cap is **10 USD**:

```toml
[campaign]
backend = "cloud"
workers = 2

[cloud]
adapter = "simulator"
cost_per_worker_minute = 0.05
estimated_shard_minutes = 1.0

[spend]
max_spend = 10.0
currency = "USD"
overrun = "drain" # drain | cancel
```

At the sample rate, 10 USD is 200 worker-minutes of estimates before new shards stop. The cap is a ceiling on estimates, not a provider invoice.

`budgets.on_budget` and `spend.overrun` say what happens to work already running when a cap or a clock trips:

| Policy | In-flight workers |
| --- | --- |
| `drain` | They finish. `cancel` does not stop them. |
| `cancel` | `backend.cancel(worker_id)` stops that worker. On Tart that is `tart stop aqa-<worker-id>`. |

New shards are not started after `spend_cap` or `budget`, under either policy.

`max_spend` on `backend = local` stays in the config. The local rate is 0, and the orchestrator records that the cap is ignored. A VM rate of 0 with no cap is unmetered. A non-zero VM rate with `max_spend` set uses the same meter.

### A paid Mac host later

Implement `swarmqa.backends.base.RunnerBackend` (`name`, `cost_per_worker_minute`, `run_shard`, `cancel`) and register it:

```python
from swarmqa.backends.cloud import register_cloud_adapter

def factory(config):
    return PaidMacBackend(config)

register_cloud_adapter("paid-mac", factory)
```

Set `cloud.adapter = "paid-mac"`. `create_cloud_backend` returns that factory's object. The orchestrator, CLI, and campaign loop keep calling `create_backend`. An unknown adapter name raises `ChunkNotReady` naming `cloud.adapter` and `RunnerBackend`.
