# verify_fix

After a coding agent fixes a bug, verify_fix rebuilds the app and replays
the finding to check that the bug is gone. The contract is
`swarmqa.verify` (see `docs/CONTRACTS.md`):

```python
run_verify(finding_id, *, config, campaign_id=None, devices=2, build=True,
           verify_id=None, driver_factory=None) -> VerifyResult
start_verify(finding_id, *, config_path="aqa.config.toml", campaign_id=None,
             devices=2, build=True) -> VerifyResult     # state "running"
verify_status(verify_id, *, report_root=None) -> VerifyResult
```

`VerifyResult` is `swarmqa.mcp.tools.VerifyResult`: `verify_id`,
`finding_id`, `state` (`running`, `passed`, `failed`, `error`), `devices`,
`reproduced_on`, `evidence` and `message`.

From the command line (the lead wires this into `cli.py` as `aqa verify`;
the entry point is `swarmqa.verify.cli.main`):

```sh
aqa verify <finding-id> [--campaign ID] [--devices N] [--no-build] [--config PATH]
```

It prints one line per device, for example
`f-w1-3 on iPhone 15: did not reproduce (the flow ran and the finding was not seen)`,
and exits 0 when the finding did not reproduce on any device, 1 when it
still reproduces on at least one, and 2 when it could not be verified.

## Steps

1. **Resolve.** The campaign is `<report_root>/<campaign_id>`, or the latest
   campaign when none is named. The finding comes from its `findings.json`.
   The replay flow is `Finding.repro`, else `Finding.replay_json`, else
   `findings/<id>.replay.json`. A `launch` finding without one gets a
   one-step `launch` flow. Any other finding without one is an `error`.
   An unknown campaign or finding is an `error` too, and nothing is written.
2. **Build.** With `build=True` and `app.build_command` set, the command runs
   through the shell in `app.source_dir` (or the working directory) with a
   timeout (`swarmqa.verify.build.BUILD_TIMEOUT_S`, 30 minutes), logging to
   `build.log`. A non-zero exit or a timeout ends the run as `error` with the
   last 20 lines of the log in `message`. Without a build command the step is
   skipped and `message` says so. The step is `swarmqa.verify.build.build_app`;
   `core.verify(..., builder=...)` takes any function with its signature, so
   a per-SHA builder can replace it.
3. **Replay** on each device with `swarmqa.report.repro.replay_finding`,
   which applies the reproduction rules in `docs/evidence.md`. Each device
   gets its own directory and driver.
4. **Verdict.**

| State | When |
| --- | --- |
| `failed` | At least one device reproduced the finding. `reproduced_on` names them. |
| `error` | No device reproduced it, but at least one could not run the replay (the app did not launch, the driver could not be created, or the replay raised), or the build failed. |
| `passed` | Every device ran the replay and none reproduced the finding. |

A launch failure counts as "could not run" except for a `launch` finding,
where it is the reproduction.

## Devices

`devices` replays run in parallel threads, one driver each. The driver comes
from `driver_factory(target, work_dir)` when given (tests), else from
`driver.kind` in the config, as `aqa replay` makes it.

- **iOS with `app.simulators`:** device n uses the n-th distinct simulator,
  and its target is narrowed to that one (`simulators = [name]`,
  `simulator = name`), so the iOS driver claims it under the usual host-wide
  `simulator-<udid>` lock. With more devices than simulators the list wraps
  (`iPhone 15#1`, `iPhone 15#2`), and replays sharing a simulator run one
  after another. A simulator that another campaign holds makes that device
  an `error`. `SWARMQA_SIMULATOR_UDID`, when set, pins every device to one
  simulator.
- **Everything else** (macOS, or iOS without `app.simulators`): every device
  is the host, named `macos-1`, `macos-2`, ... The local Mac is one device
  with one display and one copy of the app, so with a real driver the
  replays run one after another; N replays there check for flakiness, not
  device spread. With the fake driver or an injected `driver_factory` they
  run in parallel.

Device pools (`swarmqa.devices`) are not used yet: leasing clones and
installing the fresh build belongs with the per-SHA builder and the swarm
scheduler.

## Files

Each run writes `<report_root>/<campaign_id>/verify/<verify_id>/`:

| Path | What |
| --- | --- |
| `status.json` | The `VerifyResult` plus `campaign_id`, `pid`, `phase` (`starting`, `build`, `replay`, `done`), `device_results`, `started_at`, `finished_at`, `updated_at`. Rewritten atomically as the run progresses. |
| `build.log` | Build output, when a build ran. |
| `devices/<n>-<name>/` | One device's replay: `replay_result.json` (outcome, reason, observed fingerprints, steps), screenshots, and the replay's own artifacts. |
| `runner.pid`, `runner.log` | Detached runs only: the runner's pid and output. |

`evidence` lists `build.log`, each device's `replay_result.json` and its
screenshots and other artifacts, relative to the campaign directory.

`verify_id` is `v-<UTC timestamp>-<6 hex>`.

## Detached runs

`start_verify` loads the config, resolves the campaign and finding (errors
come back at once with state `error`), writes `status.json` with state
`running`, and launches
`python -m swarmqa.verify._runner <finding-id> --config ... --campaign ... --devices N --verify-id ...`
in a new session with output to `runner.log`. It writes the child's pid to
`runner.pid` and returns. The runner calls `run_verify` with the same
`verify_id`, which writes the final `status.json`.

`start_verify` also writes `$AQA_VERIFY_INDEX/<verify_id>.json` (default
`~/.aqa/verify/`) pointing at the run directory, so `verify_status` needs
only the id. It looks in `report_root` first when given, then the index,
then `./reports`. If `status.json` still says `running` but its pid (from
`status.json`, else `runner.pid`) is no longer alive, `verify_status`
reports `error` with the tail of `runner.log`. Runners started by the
calling process are reaped on each `verify_status` call so they do not
linger as zombies.
