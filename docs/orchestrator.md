# Orchestrator and local backend

The orchestrator turns a shard queue into one campaign. It runs at most `config.workers` shards at a time, merges their artifacts into a single report, and keeps going when one shard crashes.

`run_campaign(config, queue, *, options=None, driver_factory=None, executor=None)` returns a `CampaignResult`. The CLI calls it without `driver_factory` or `executor`. Tests pass those keyword-only hooks.

## Campaign directory

New runs call `new_campaign_id()` and create `config.report_root/<id>/` with `ensure_campaign_layout`. `options.resume_campaign_id` reuses that directory when it is present.

Resume reads `status.json` and reruns shard ids listed in `failed` and `pending`. Ids in `completed` or `cancelled` are kept and not started again. Shard ids the status file has never seen are treated as new work. Spend in `spend_estimated` is added back onto the meter unless `options.reset_spend` is true. Worker ids continue past directories that already exist so a rerun does not overwrite `workers/<id>/result.json`.

## Scheduling

The scheduler asks `CampaignClock.can_schedule` before it starts another shard. For a metered backend (`cost_per_worker_minute > 0`) it also asks `SpendMeter.can_start(rate * config.cloud.estimated_shard_minutes)` and reserves that estimate when the shard starts. A refusal stops scheduling. `coverage.stop_reason` is `budget`, `spend_cap`, or null when the queue empties. Shards left in the queue are `not_started` and stay in `status.json` `pending` so a later resume can run them.

`budgets.on_budget` and `spend.overrun` choose what happens to work already running:

- `drain` waits for those shards to finish.
- `cancel` calls `backend.cancel(worker_id)`. Local subprocess workers and in-process suite commands are terminated. The shard is recorded as `cancelled`.

A shard that raises is stored as `status="error"` with the exception text. The worker slot is released and the rest of the queue continues. `ChunkNotReady` from scripted or exploratory explorers is caught the same way, so a fleet still writes its report when those modules are not ready.

Coverage counts:

- `completed`: `passed`
- `failed`: `failed` or `error`
- `cancelled`: `cancelled`
- `not_started`: selected shards that never started

## Local backend and spend

`LocalBackend.cost_per_worker_minute()` is `0`. When `backend` is `local` and `spend.max_spend` is set, the campaign still schedules every shard that the clock allows. `SpendSummary.note` is exactly `max_spend is ignored on the local backend`. The cap is still shown on the progress line and in the summary.

`executor`, when passed to `LocalBackend` or `run_campaign`, replaces dispatch for that process. It is called as `executor(shard, worker_id, work_dir, config) -> WorkerResult`. The default path uses `driver_factory(target, work_dir)` when one was provided, otherwise `swarmqa.driver.create_driver`.

Default dispatch:

| Kind | Action |
| --- | --- |
| `scripted` | `run_scripted` |
| `exploratory` | `run_exploratory` |
| `suite` | shell out to `shard.suite_command` or `config.suite.command`; exit 0 is `passed`, anything else is `failed`; stdout and stderr go under `raw/` |
| `visual` | screenshot each `visual_names` entry and `compare_screenshot` against `visual.baseline_dir` |

`config.local.isolation` defaults to `thread` (the shard runs inside the worker thread). `subprocess` runs `python -m swarmqa.worker` and reads `workers/<id>/result.json`. An injected `executor` stays in-process so tests do not have to fork.

## Worker process

```bash
python -m swarmqa.worker \
  --campaign-dir reports/<id> \
  --shard-file workers/<id>/shard.json \
  --config-json workers/<id>/config.json \
  --worker-id w1
```

The process writes `workers/<id>/result.json`. It exits `0` when that file exists, including when the shard status is `failed` or `error`. It exits `1` only when the result file could not be written.

## Status and progress

`write_status` stores `report_dir/status.json` while the run is in progress (queue depth, active workers, spend used and cap). `read_status(report_root, campaign_id)` prints the same lines `aqa status` shows. Omit `campaign_id` to use the latest campaign directory.

```text
[campaign <id>] queue=<n> active=<n> backend=<name> spend=<spent>/<cap> <currency>
[worker <id>] shard=<name> mode=<kind> step=<action>
```

`<cap>` is `none` when `max_spend` is unset. Amounts are printed without trailing zeros.

## One report

Findings from every worker go through `dedup_findings` (same fingerprint, union of screenshots, videos, and worker ids). `write_summary` then writes a single `summary.md` and `summary.json` for the campaign.

Exit code:

- `fail_on=scripted`: `1` when a scripted or suite shard `failed`, or any shard is `error`. Exploratory and visual findings stay `0`.
- `fail_on=any`: `1` when any finding was recorded.
- `fail_on=never`: `0` after the report is written.

## GUI warning

On stderr, when `backend` is `local`, `workers` is greater than `gui_worker_warn_threshold`, and the queue contains a scripted, exploratory, or visual shard:

```text
warning: local backend has N workers, above the GUI threshold of T. Scripted, exploratory, and visual shards share this display and session. Use the vm or cloud backend for isolated GUI sessions.
```

Suite-only campaigns do not warn. Local workers share one display; `vm` or `cloud` gives each worker its own session.
