# Device pools

A device pool leases devices to workers. The contract is `swarmqa.devices.protocol` (see "Phase 1 contracts (v2)" in `docs/CONTRACTS.md`): `acquire(platform, build, *, timeout_s)` returns a booted device with the build installed or raises `DeviceUnavailable`; `release(device, *, erase=True)` is idempotent; `close()` releases every lease and removes what the pool created. Use `with lease(pool, platform, build) as device:` so a crashed worker still releases.

| Pool | Module | Platform | Device |
| --- | --- | --- | --- |
| `IOSSimulatorPool` | `swarmqa.devices.ios_pool` | `ios` | `kind="simulator"`, `id=<udid>`, `address=""` |
| `TartVMPool` | `swarmqa.devices.tart_pool` | `macos` | `kind="vm"`, `id=aqa-vm-<slot>`, `address=<guest ip>` |
| `LocalMacPool` | `swarmqa.devices.local_mac` | `macos` | `kind="host"`, `id="host"`, `address=""` |
| `FakeDevicePool` | `swarmqa.devices.fake` | any | in-memory, tests only |

Every pool runs host commands through an injectable `runner(args, **kwargs)` (`swarmqa.devices.commands`). The tests (`tests/test_devices.py`) replay recorded `simctl`, `tart` and `sysctl` output on Linux, so the commands below still need checking on a real Mac.

## Host-wide locks

`swarmqa.devices.locks` takes an exclusive `fcntl.flock` on `<root>/<name>.lock`. The root is, in order: the `root` / `lock_root` argument, `$AQA_LOCK_DIR`, then `~/.aqa/locks`. The kernel drops a lock when its holder exits, so a crashed worker never leaves a stale lease. Lock files stay on disk; only the flock is the lease, and the PID written inside is informational.

| Lock name | Held by | Meaning |
| --- | --- | --- |
| `ios-sim-slot-<n>` | `IOSSimulatorPool` | one of the host's simulator capacity slots |
| `simulator-<udid-or-name>` | `IOSSimulatorPool`, `IOSSimulatorDriver` | one simulator is in use |
| `tart-vm-slot-<n>` (n is 0 or 1) | `TartVMPool`, `TartBackend` | one running Tart VM |
| `local-mac` | `LocalMacPool` | the local Mac is leased |

`HostLock.try_acquire()` never blocks. `HostLock.acquire(timeout_s)` and `acquire_slot(prefix, count, timeout_s)` poll and raise `LockTimeout`. Two `HostLock`s for the same name exclude each other even inside one process.

The legacy iOS driver (`driver/ios.py`) takes `simulator-<name>` for each entry in `app.simulators` instead of its old per-campaign `.ios-simulators/` lock files. A driver given a pinned UDID (`udid=` or `SWARMQA_SIMULATOR_UDID`) takes no lock, because the pool that handed out the UDID already holds `simulator-<udid>`.

## Capacity

`swarmqa.devices.capacity` reads RAM and cores through the runner seam:

```
sysctl -n hw.memsize        # macOS
sysctl -n hw.ncpu           # macOS
os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"), os.cpu_count()   # elsewhere
```

`CapacityPolicy` defaults: 4 GB reserved for the host, then one simulator per 2.5 GB and per 2 cores, and one VM per 8 GB and per 4 cores. The tighter bound wins and the result is never below 1. `max_simulators` and `max_vms` cap it. Tart is always capped at **2** VMs per host (`TART_VM_LIMIT`): Apple's macOS licence allows two virtual macOS instances per Mac, and Virtualization.framework refuses a third.

## IOSSimulatorPool

```python
IOSSimulatorPool(golden="iPhone 16 Golden")            # clone a shut-down golden device
IOSSimulatorPool(device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-16",
                 runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-0")   # or create fresh
```

Slot `n` owns a clone named `aqa-sim-<source>-<n>`, where `<source>` is the golden name or the short device type and runtime. Commands, in order:

**acquire**

1. `xcrun simctl list devices -j` — find an existing clone with that name (a leftover from an earlier run is reused and never deleted by this pool). Run once per slot per pool.
2. When there is none: `xcrun simctl clone <golden> <clone-name>`, or `xcrun simctl create <clone-name> <device_type> [<runtime>]`. The last line of stdout is the new UDID. `simctl clone` needs the golden device shut down.
3. `xcrun simctl bootstatus <udid> -b` — boots the clone if needed and blocks until it has finished booting. The runner gets `timeout=boot_timeout_s` (default 600 s); failure or timeout raises `DeviceSetupError`.
4. With a build: `xcrun simctl install <udid> <build.app_path>`.

Before step 3 the pool holds `ios-sim-slot-<n>` and `simulator-<udid>`. If another process holds that UDID it tries the next slot. With every slot busy it polls every `poll_s` until `timeout_s`, then raises `DeviceUnavailable`. Any failure in steps 2–4 releases both locks.

**release** with `erase=True` and `erase_mode="uninstall"` (the default): `xcrun simctl uninstall <udid> <build.bundle_id>`. The clone stays booted, so the next lease skips the boot. Uninstalling removes the app's data container; the keychain and system settings survive.

With `erase_mode="erase"`, for a fully clean device on every lease:

1. `xcrun simctl shutdown <udid>` — `simctl erase` refuses a booted device. "Unable to shutdown device in current state: Shutdown" is treated as success.
2. `xcrun simctl erase <udid>` — wipes all content and settings, including the app. The clone stays for the next lease, which boots it again. If erase fails, the clone is retired (`xcrun simctl delete <udid>` when this pool created it).

With `erase=False` nothing runs and the app stays installed.

**close**: releases open leases, then for each clone this pool created, `xcrun simctl shutdown <udid>` and `xcrun simctl delete <udid>`.

## TartVMPool

```python
TartVMPool(image="ghcr.io/cirruslabs/macos-sequoia-base:latest")
```

Slot `n` (0 or 1) owns a VM named `aqa-vm-<n>`. It holds `tart-vm-slot-<n>`, the same slots `TartBackend` uses, so the pool and the backend together never run more than two VMs.

**acquire**

1. `tart list --format json` — is `aqa-vm-<n>` already there?
2. When it is not: `tart clone <image> aqa-vm-<n>` (APFS copy-on-write; pulls the image the first time).
3. `tart run --no-graphics [--dir=aqa-build:<dirname of build.app_path>] aqa-vm-<n>`, started in the background. The guest sees the build at `/Volumes/My Shared Files/aqa-build/<App>.app`, reported as `device.meta["guest_app_path"]`.
4. Poll `tart ip aqa-vm-<n>` until it prints an address, then `tart exec aqa-vm-<n> true` until it exits 0 (the guest agent answers). Poll interval `poll_s` (2 s); after `boot_timeout_s` (300 s) it runs `tart stop` and raises `DeviceUnavailable`.

**release**: `tart stop aqa-vm-<n>`; with `erase=True` also `tart delete aqa-vm-<n>`, so the next lease starts from a fresh clone of the image.

**close**: stops open leases without deleting, then `tart delete` for each VM this pool cloned.

## LocalMacPool

One device, the local Mac, leased with the `local-mac` lock. macOS apps run in place from `build.app_path`, so nothing is installed. Capacity is 1 for `macos` on `darwin` and 0 otherwise.

It runs no commands by default. The local Mac is the user's own machine, so `erase=True` only resets app state when the pool was built with `reset_app_state=True`, and then runs:

```
defaults delete <build.bundle_id>
```

## TartBackend

`TartBackend.run_shard` (see `docs/backends.md`) now:

- takes a `tart-vm-slot-<n>` lock before cloning, waiting up to `slot_timeout_s` (1800 s); when none frees up the shard ends `error`, saying the host runs at most two macOS VMs
- after `tart run`, polls `tart ip aqa-<worker-id>` and then `tart exec aqa-<worker-id> true` until both succeed or `boot_timeout_s` (300 s) passes; a timeout ends the shard `error` and stops the VM
- treats a missing or unreadable `workers/<worker-id>/result.json` as `error`, never `passed`
- writes into the running campaign's directory (see below)
- copies the `swarmqa` package, the `.app` bundle and pulled artifacts with symlinks preserved

The orchestrator calls `run_shard(shard, worker_id)` without a campaign directory. The backend uses, in order: the `campaign_dir` argument, the constructor's `campaign_dir`, the running campaign under `report_root` that already has `workers/<worker-id>` (the orchestrator creates it before `run_shard`), then `report_root/vm`.

## Checked on a Mac

Run by hand on an 8 GB, 8-core Apple Silicon Mac with Xcode 26.5 and the iOS 26.5 runtime, while two `xcodebuild` jobs were running:

| Command | Result |
| --- | --- |
| `simctl create <name> <type> <runtime>`, `simctl clone <golden> <name>` | exit 0; stdout is only the new UDID |
| `simctl list devices -j` | entries carry `name`, `udid`, `state`, `isAvailable` |
| `simctl shutdown` on a shut-down device | exit 149; stderr ends `Unable to shutdown device in current state: Shutdown` |
| `simctl bootstatus <udid> -b` | boots a shut-down device and exits 0 once booted; exits 0 at once when already booted |
| `simctl erase` on a booted device | exit 149, `Unable to erase contents and settings in current state: Booted` |
| `simctl uninstall` of an app that is not installed | exit 0 |
| `simctl install` of a missing path | exit 2 |
| `simctl delete` on a booted device | exit 0 |
| `sysctl -n hw.memsize`, `sysctl -n hw.ncpu` | plain integers (`8589934592`, `8`) |

Boot times: 221 s for a new clone's first boot, **336 s** for the boot after `simctl erase`, and 87 s for a warm reboot. An erased clone boots like a new one, so `erase_mode="erase"` makes every lease pay a cold boot. The 300 s default timeout was too short, so it is now 600 s, and `erase_mode` now defaults to `"uninstall"`.

## Not yet verified on a Mac

Tart was not installed on the test Mac, so these still come from the Tart documentation:

- `tart list --format json` field names (`Name`), `tart run --no-graphics`, and the `--dir=name:path` mount path under `/Volumes/My Shared Files/`.
- `tart ip` exits non-zero until the guest has an address, and `tart exec` needs the Tart guest agent in the image.
- Whether 2.5 GB and 2 cores per simulator suit real campaigns.
