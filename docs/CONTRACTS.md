# Implementation contracts

Read `docs/autonomous-qa-plan.md` for product intent and `docs/OWNERSHIP.md` for file ownership. This file is the build contract. Locked defaults:

- `pr.mode = human`
- `workers = 2`
- `video.mode = always`
- cloud sample cap `max_spend = 10`, `currency = USD`
- recorded flows are the owned JSON action list (`docs/flow.schema.json`, version 1)
- one fix/PR loop per campaign
- v1 backends are `local` and Tart `vm`; `cloud` is a pluggable metered adapter with a local simulator

Tests run on Linux. Use `FakeDriver` (`swarmqa.driver.fake`) for UI behavior. macOS and Tart code must import on Linux and fail with a clear, actionable error when the host cannot run them.

Exit codes from `aqa`: `0` success, `1` scripted failure or unhandled worker crash, `2` usage or config error. Exploratory-only findings stay exit `0` when `fail_on = scripted`. `fail_on = any` returns `1` when any finding exists. `fail_on = never` returns `0` unless the campaign itself crashes before writing a report.

## Shared objects

`CampaignConfig`, `Shard`, `Action`, `Finding`, `WorkerResult`, `CampaignResult`, `CampaignStatus`, and `FixLoopResult` live in `swarmqa.models`. Serialize with `swarmqa.serialize.dump_json`.

Report directories (`swarmqa.report.layout.ensure_campaign_layout`):

```
reports/<campaign-id>/
  summary.md
  summary.json
  status.json
  workers/<id>/
  findings/
  media/
  raw/
  pr/
```

Write summaries with `swarmqa.report.summary.write_summary`. Dedup with `swarmqa.report.dedup.dedup_findings` (same fingerprint, union of screenshots, videos, and `worker_ids`).

Spend: `swarmqa.spend.SpendMeter`. Budgets: `swarmqa.budgets.CampaignClock`. Video: `swarmqa.video_policy`. Durations: `swarmqa.util.parse_duration` (`90s`, `5m`, `2h`, `1h30m`).

Element matching (`swarmqa.driver.query`): role equality, case-insensitive; label substring, case-insensitive; identifier and value exact.

Fingerprint algorithm (implement in `fingerprint_for`): SHA-256 of `kind + "\\n" + title.strip().lower() + "\\n" + target.strip().lower()`, first 16 hex characters.

Issue template placeholders: `{{title}}` `{{severity}}` `{{kind}}` `{{steps}}` `{{video}}` `{{screenshots}}` `{{replay_json}}` `{{environment}}` `{{worker_id}}` `{{backend}}` `{{build_id}}` `{{fingerprint}}` `{{details}}`.

`steps` and `screenshots` are newline-joined. `environment` is `key: value` lines. `build_id` comes from `environment["version"]` or empty. Unknown placeholders stay as written. Missing values become empty strings. When `config.issues.template` is unset, use `swarmqa/templates/issue.md`.

## C1 — Config

Implement `load_config(path, overrides) -> CampaignConfig` and `validate_config(config) -> list[str]`.

TOML shape is `swarmqa/templates/aqa.config.toml`. Map sections onto `CampaignConfig` fields. `max_wall_time` strings go through `parse_duration` into `budgets.max_wall_time_s` and `pr.max_wall_time_s`. Top-level `intents` is a string array. Accept intent paths that point at a file or a directory.

CLI overrides replace config for one run: `--app`, repeated `--intent`, `--backend`, `--workers`, `--max-wall-time`, `--max-spend`, `--spend-currency`, `--video-mode`, `--pr-mode`.

Validation, each message prefixed with its field path:

- `backend` is `local`, `vm`, or `cloud`
- `workers` is an integer `>= 1`
- `video.mode` is `always`, `on_failure`, or `exploratory_only`
- `pr.mode` is `human` or `autonomous`
- `app.maturity` is `prototype` or `shipped`
- `visual.threshold` is between 0 and 1 inclusive
- `spend.currency` is a non-empty string
- `backend = cloud` requires `spend.max_spend` present and `> 0`
- `pr.max_iterations` and `pr.max_pr_updates` are `>= 1`
- `explorer.max_steps` is `>= 1` and `explorer.max_time_s` is `> 0`
- unknown backend, video mode, or PR mode is a field error

`load_config` raises `ConfigError` whose `errors` list is those messages, one per line via `str(error)`. A missing config file raises `ConfigError(["config: not found"])`.

`max_spend` on `backend = local` is valid. Leave the number in place.

Invalid TOML raises `ConfigError(["config: invalid toml"])`.

## C2 — macOS driver

Implement `MacOSDriver` so it satisfies `AppDriver`. On non-darwin platforms, `launch` raises `BackendUnavailable` (or `AppMissingError`) whose message tells the caller to use the fake driver or a Mac host. Importing the module on Linux must succeed.

On macOS the driver:

- launches a `.app` bundle from `target.path` (binary inside `Contents/MacOS` or `open`), and runs `target.build_command` first when `path` is empty
- raises `AppMissingError` when the bundle is missing
- raises `AppCrashedError` when the process exits during launch or action
- reads the accessibility tree into `UIElement` nodes (windows, buttons, text fields, menus)
- performs click, type, keychord, scroll, menu select, and `wait_for` with timeout via System Events / osascript (or Accessibility APIs when importable)
- uses `swarmqa.driver.query` matching semantics
- writes a PNG screenshot under the worker media directory
- starts and stops a session video when `ffmpeg` or `screencapture` is available; if neither exists, `start_video` records that video is unavailable by writing a `.txt` note beside the session and `stop_video` returns that path
- `metadata()` fills path, bundle id, and `CFBundleShortVersionString` when Info.plist can be read
- `relaunch` quits and launches again
- documents Accessibility permission and the isolation assumption in `docs/driver.md`: one session per worker, no exclusive host lock, orchestrator places the driver in a VM when true GUI parallelism is required

Inject subprocess calls behind an instance attribute so tests can fake osascript output on Linux. Tests must pass on Linux without a display.

## C3 — Intent ingestion

`build_queue(config) -> list[Shard]`:

- Expand each path in `config.intents`. Directories include `*.md` and `*.json` files, non-recursive is acceptable, recursive is better. Missing paths raise `IntentError`.
- A `.json` file is a version-1 flow: one `kind="scripted"` shard whose `actions` are the steps. Reject other versions with `IntentError`.
- A `.md` file becomes one shard. If it contains a `## Steps` list using the grammar below, `kind="scripted"` and those actions are filled. Otherwise `kind="exploratory"` and `goal` is the title plus body prose.
- Constraints under `## Constraints` become `shard.constraints`.
- When `coverage.exploratory` is true and the markdown shard is scripted, also emit one exploratory shard with the same goal and `seed="explore"`.
- When `coverage.scripted` is false, drop scripted shards. When `coverage.exploratory` is false, drop exploratory shards.
- When `config.suite.command` is set, append one `kind="suite"` shard with that command.
- When `config.visual.enabled` or `coverage.visual`, append one `kind="visual"` shard. `visual_names` lists `*.png` stems in `visual.baseline_dir` when that directory exists.
- Shard ids look like `s-<n>-<slug>` using `swarmqa.util.slug`.
- An empty queue raises `IntentError` with a message that the intent set is empty.
- `shard_strategy` orders the queue: `intent` keeps file order; `suite` places suite shards first; `exploratory_seed` places exploratory shards first.

Markdown step grammar (one action per bullet):

```
- click "Label"
- click button "Label"
- type "text" into "Label"
- key cmd+return
- menu File > New
- wait for "Label" 5s
- scroll -3
- screenshot "name"
- assert "Label" exists
- relaunch
```

`record_flow(...) -> Path` writes pretty JSON matching `docs/flow.schema.json`:

- When `events` is a list of action dicts, write those steps.
- When `interactive` is true, read stdin lines using the same bullet grammar without the leading hyphen, until a line `done` or EOF.
- Otherwise write a document that contains a single `launch` step and a note is not required; still return a valid file.
- Print path is the caller's job. The function returns the path it wrote.
- Hand-authored JSON must load through `build_queue` without using `record_flow`.

## C4 — Scripted explorer and reporter

`run_scripted(shard, driver, config, *, worker_id, work_dir) -> WorkerResult`:

- `driver.launch()` first. Launch failures become a finding `kind="launch"`, status `failed`.
- Honor `video_policy.should_start_video`. After the shard, delete the video file when `keep_video` is false.
- Each action maps to a driver call. Append a `StepResult`.
- `assert` with `exists=true` passes when the element is found and fails when it is missing. `exists=false` is the opposite.
- `screenshot` uses `action.name` or `step-<index>`.
- On `AppCrashedError`, `UITimeoutError`, or `ElementNotFoundError`, record a finding and a failed step. Stop the chain when `explorer.on_step_failure == "stop"`; continue when it is `continue`.
- A failed scripted shard has `status="failed"`. A clean shard has `status="passed"` and no findings.
- Attach `BuildMetadata` fields on `finding.environment` (`path`, `bundle_id`, `version`).
- Severity guess: crash `critical`, launch `critical`, timeout `high`, missing control `high` when `app.maturity == "prototype"` else `medium`, assertion `medium`.
- Call `write_finding` and `write_replay` for each finding. Replay JSON is the executed actions including the failing one, wrapped as a version-1 flow. Set `finding.replay_json` and screenshot/video paths relative to the campaign directory when the work dir is inside one, otherwise absolute.
- Put worker media under `work_dir`.

`fingerprint_for`, `write_finding`, `write_replay`, `render_issue`, `default_template` per the shared section. Finding markdown includes title, severity, steps, evidence paths, worker id, and backend.

`create_issues`:

- Always write the rendered ticket to `findings/<id>.md` (via `write_finding` if not already present) and return an `IssueRef(tracker="local", ...)`.
- When `issues.github` is true, run `gh issue create --repo <repo> --title <title> --body <body>` through `runner` (default `subprocess.run`). Body includes the video path, screenshot paths, and a fenced replay JSON stub. Record the URL from stdout. Do not put tokens in files. Pass `GH_TOKEN` through the environment from `issues.github_token_env` when that variable is set.
- When `issues.linear` is true, POST GraphQL `issueCreate` to `https://api.linear.app/graphql` using `http_post(url, headers, body) -> dict`. Authorization header is the env var named by `issues.linear_api_key_env`. Team id is `issues.linear_team`. Description includes the same evidence links.
- Tracker failures become `details` on the local finding file and do not raise past the campaign.

## C5 — Exploratory hunting

`run_exploratory(...) -> WorkerResult` within `explorer.max_steps` and `explorer.max_time_s`:

- Launch the app. A crash or missing bundle is a finding, same severity rules as C4.
- Hunt loop is observe → decide → act. `swarmqa.decision.build_evaluator(config)` supplies the `DecisionEvaluator`; tests may pass `evaluator=` to inject one. Default `explorer.decision.mode = heuristic` preserves the classic strategy order. Opt-in `system_one` / `cascade` escalate to System One on stall and fail open; see `docs/decision.md` (privacy: a11y labels leave the machine when System One HTTP is enabled).
- Heuristic strategies, in order, stopping when the evaluator returns done or the budget ends: search the accessibility tree for labels that share a word with the goal; click enabled buttons in that set; try menu bar paths that match goal words; if `maturity == "prototype"` and an expected-looking control from the goal is absent, emit `missing_control` (a finding, not a silent skip).
- Detect `AppCrashedError` (`crash`), `UITimeoutError` (`unresponsive`), and elements whose label or value contains `error` or `empty` (`error_state`).
- Record a short hypothesis string in `finding.details` (for example `Settings gear missing — trying menu bar`).
- When `explorer.friction.enabled`, meter path cost and may emit advisory `friction_path` (see C13 / `docs/friction.md`). `fail_ci` is reserved and not wired to `fail_on`.
- Status is `passed` when no findings were recorded, `failed` when findings exist.
- Exploratory shards still write finding files and a replay stub of the actions the explorer actually performed.
- Log hypotheses to the worker result `error` field only for a fatal worker fault; hypotheses belong in finding details.

Use the fake driver in tests. A half-wired tree that lacks a goal control must yield at least one finding.

## C13 — UX friction (advisory)

`swarmqa.friction` meters gold-relative session metrics on exploratory hunts and may emit `Finding.kind = friction_path`. Titles lead with a number. Default gates (`min_extra_steps = 3`, synthesized gold score ≥ 65) keep short happy-path FakeDriver hunts silent. Report summaries list friction under `## Friction (advisory)` only. Under `fail_on = scripted`, exploratory friction stays exit 0 like other exploratory findings. See `docs/friction.md`.

## C6 — Visual diff

`compare_screenshot(current, baseline, diff_out, *, threshold) -> DiffResult`:

- Load both images with Pillow. Compare the fraction of pixels whose per-channel absolute delta is greater than 16 after scaling the current image to the baseline size.
- `passed` when the fraction is `<= threshold`. `score` is that fraction.
- Write `diff_out` as a PNG with changed pixels highlighted. Never write into the baseline path.
- Missing baseline returns `passed=False` and a message that the baseline is missing.

`update_baselines(source_dir, baseline_dir) -> list[Path]` copies `*.png` from the source into the baseline directory, creating it if needed. This is the only write path for baselines.

Tag visual findings `kind="visual"` when the orchestrator or a caller wraps `DiffResult` in a `Finding`. Concurrent compares only read baselines.

## C7 — Fix and PR loop

`run_fix_loop(result, config, *, repo, fixer=None, retest=None, gh_runner=None) -> FixLoopResult`.

There is exactly one loop per campaign. Ignore worker identity when opening a PR.

Human mode (`pr.mode == "human"`):

- Create branch name `aqa/<campaign-id>` in `repo` when `repo` is a git checkout. If git is unavailable, skip the branch and still write files.
- Write `report_dir/pr/draft.md` from the merged findings (title, body, failing shards, evidence paths, video, replay).
- When `gh_runner` is provided or `gh` exists on PATH, create a draft PR (`gh pr create --draft`). Store the URL on the result.
- Do not apply code fixes. `stop_reason="human"`. Return after that single draft.

Autonomous mode:

- Repeat while findings remain and all caps hold: `iterations < pr.max_iterations`, `pr_updates < pr.max_pr_updates`, and elapsed time `< pr.max_wall_time_s`.
- Ask `fixer(findings, repo, iteration) -> FixProposal`. When `fixer` is omitted, use `config.pr.fix_command` or env `AQA_FIX_COMMAND`. When no command exists, write the draft, set `stop_reason="no_fixer"`, and return with remaining finding ids. Do not spin.
- Open or update one PR for the campaign branch.
- Call `retest(failed_shards) -> CampaignResult` on the shards that failed, not the original full queue, unless the caller passes a retest that does otherwise.
- When retest reports no failed scripted shards, `stop_reason="green"`.
- When a cap trips, leave `stop_reason` as `max_iterations`, `max_pr_updates`, or `max_wall_time`, list `remaining_finding_ids`, and keep the PR open (do not close it).

`gh_runner(args: list[str]) -> subprocess.CompletedProcess` is the test seam. Never merge to the default branch.

## C8 — Orchestrator and local backend

`run_campaign(config, queue, *, options=None, driver_factory=None) -> CampaignResult`:

- Create a campaign directory under `config.report_root` with `new_campaign_id()`, or reuse `options.resume_campaign_id` when that directory exists.
- Resume reruns shards whose ids are in `status.json` under `failed` and `pending`. Completed shard ids are skipped. Spend in the status file carries forward unless `options.reset_spend` is true.
- Run at most `config.workers` shards at once through `LocalBackend` when `config.backend == "local"`.
- `LocalBackend.cost_per_worker_minute()` stays `0`. When `spend.max_spend` is set on local, set `SpendSummary.note` to `max_spend is ignored on the local backend` and do not block on spend.
- Before each schedule, consult `CampaignClock.can_schedule` and, for metered backends, `SpendMeter.can_start(rate * cloud.estimated_shard_minutes)`. On refusal, stop scheduling. `on_budget` / `spend.overrun` of `drain` lets in-flight shards finish; `cancel` calls `backend.cancel`.
- A shard that raises is `status="error"` (or `failed`), the worker slot frees, and the campaign continues.
- Dispatch by shard kind: scripted -> `run_scripted`, exploratory -> `run_exploratory`, suite -> subprocess of `suite_command` with exit code mapped to pass/fail and logs under `raw/`, visual -> compare baselines. Suite and visual may call the reporter helpers to build findings.
- Collect worker artifacts under `workers/<id>/`.
- Dedup findings across workers before writing the campaign summary.
- Coverage counts: completed (passed), failed (failed or error), cancelled, not_started. `stop_reason` is `budget`, `spend_cap`, or null when the queue empties.
- Exit code follows the `fail_on` rules.
- Warn on stderr when `backend == local`, `workers` exceeds `gui_worker_warn_threshold`, and any shard kind is scripted, exploratory, or visual. Mention that vm or cloud isolates GUI sessions.
- Update `status.json` during the run via `write_status`. Include queue depth, active workers, spend used and cap.
- Live lines on stderr: `[campaign <id>] queue=<n> active=<n> backend=<name> spend=<spent>/<cap> <currency>` and `[worker <id>] shard=<name> mode=<kind> step=<action>`.
- `read_status` prints that same information for `aqa status`.

`python -m swarmqa.worker --campaign-dir ... --shard-file ... --config-json ... --worker-id ...` writes `workers/<id>/result.json` and returns `0` when the result file exists, including scripted failures. Return `1` only when the result file could not be written.

Thread isolation is the default. Subprocess isolation shells out to that module when `config.local.isolation == "subprocess"`.

Use `driver_factory(target, work_dir)` when provided, otherwise `swarmqa.driver.create_driver`.

## C9 — Tart and cloud

`TartBackend.ensure_available()`:

- When `sys.platform != "darwin"` or the machine is not ARM, raise `BackendUnavailable` telling the user to set `backend = local`.
- When the `tart` binary is missing, raise `BackendUnavailable` with the same guidance plus a pointer at Tart install docs in `docs/backends.md`.

`run_shard` with an injected `runner(args, **kwargs)` (tests pass a fake):

- Clone or reuse a VM named `aqa-<worker-id>` from `config.vm.image` when set.
- Copy the worker entry and shard JSON, run `python -m swarmqa.worker` inside the guest, pull `workers/` and `media/` back to the campaign directory, then stop or recycle according to `config.vm.recycle`.
- A runner failure marks that shard failed and raises only for the shard, so the orchestrator can continue with other VMs.
- `cost_per_worker_minute` returns `config.vm.cost_per_worker_minute`.

`SimulatedCloudBackend.run_shard` executes the shard with the local driver path (call the same functions the local backend uses, or `LocalBackend`) and sets `WorkerResult.estimated_cost` from the rate times the measured worker minutes (minimum one `estimated_shard_minutes` for the pre-check). `create_cloud_backend` returns the simulator for adapter `simulator`.

Document that a future paid Mac host implements `RunnerBackend` and is selected by `cloud.adapter` without editing the orchestrator.

The orchestrator already refuses to schedule when the next estimated shard would exceed `max_spend`. Tests for C9 should show two simulator shards with a cap that allows only one, `stop_reason == "spend_cap"`, and in-flight policy `drain` versus `cancel`. If orchestrator spend checks are not landed yet, implement the cap check inside the cloud backend's campaign helper only as a fallback function `plan_affordable(shards, meter, rate, minutes) -> (runnable, stopped)` in `swarmqa/backends/cloud.py` and unit-test that function so the cap math is proven without waiting on C8. Prefer calling `SpendMeter` rather than a second implementation.

## C10 — iOS Simulator driver

Implement `IOSSimulatorDriver` so it satisfies `AppDriver`. On non-darwin platforms, `launch` raises `BackendUnavailable` whose message tells the caller to use the fake driver or a Mac with Xcode. Importing the module on Linux must succeed.

`app.platform` is `macos` (default) or `ios`. `app.simulator` is one device name or UDID. `app.simulators` is a list of those, one per concurrent worker. `create_driver` selects `IOSSimulatorDriver` when `kind="ios"` or `app.platform` is `ios`.

On a Mac the driver:

- resolves a Simulator with `xcrun simctl list`, or uses a UDID from `udid=`, `SWARMQA_SIMULATOR_UDID`, `app.simulator`, or the default name `iPhone 17`
- when `app.simulators` lists more than one device, claims a free entry with a lock under the worker parent directory and releases it on `close`; a dead lock pid is reclaimed
- boots the Simulator (`boot` that reports the device is already booted is success), installs the `.app`, and launches `app.bundle_id` or `CFBundleIdentifier` from `Info.plist` (bundle root, or `Contents/Info.plist`)
- passes `launch_args` to `simctl launch` and `app.env` as `SIMCTL_CHILD_*`
- raises `AppMissingError` when the bundle is missing, install fails, or no bundle id is available
- raises `AppCrashedError` when launch crashes or the pid is gone
- reads the accessibility tree from `idb ui describe-all` into `UIElement` nodes
- performs click, type, keychord, scroll, and menu select through `idb` (`tap`, `text`, `key`, `swipe`); menu select taps each label in order; modifier keys in a chord are dropped and the remaining HID key is sent
- `wait_for` polls until `timeout_s` and then raises `UITimeoutError`
- writes a PNG screenshot with `simctl io screenshot` under the worker media directory
- starts and stops session video with `simctl io recordVideo`; if recording exits immediately, `start_video` writes `session.txt` noting that video is unavailable and `stop_video` returns that path
- `metadata()` fills path, bundle id, and `CFBundleShortVersionString` and sets `backend="ios"`
- `relaunch` terminates and launches again
- `close` is safe to call more than once; it stops video, terminates the app, and does not shut the Simulator down
- when `idb` is missing, UI actions raise `BackendUnavailable` and name `brew tap facebook/fb` plus `brew install facebook/fb/idb`. `simctl ui` is not a substitute: it only sets appearance, contrast, and content size
- when `xcrun` is missing, `launch` raises `BackendUnavailable` and names Xcode

Inject subprocess calls behind an instance attribute so tests can fake simctl and idb output on Linux. Tests must pass without a display or a booted Simulator.

A local campaign with `platform = ios`, more than one worker, and fewer `app.simulators` entries than workers warns on stderr that each worker needs its own Simulator. A pool that covers the worker count does not warn. macOS campaigns keep the existing shared-display warning.

## Progress log

stderr lines are part of the contract for C8. Other chunks write findings to disk and return structured results.
