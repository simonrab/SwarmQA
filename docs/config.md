# Campaign configuration

`aqa init` writes `aqa.config.toml` from `swarmqa/templates/aqa.config.toml`. One file describes the app, the intent set, coverage, budgets, video, issue trackers, and how hard the fleet should push: backend, worker count, and the cloud spend cap. CLI flags replace those values for a single run.

`swarmqa.config.load_config(path, overrides)` reads that file, applies `CliOverrides`, and returns a `CampaignConfig`. `validate_config(config)` returns field-level messages. An empty list means the config can run. `load_config` raises `ConfigError` with those messages instead of starting a campaign. `str(error)` is the messages joined by newlines. `aqa` exits `2` on `ConfigError`.

## Locked defaults

| Knob | Default |
| --- | --- |
| `backend` | `local` |
| `workers` | `2` |
| `pr.mode` | `off` |
| `driver.kind` | `auto` |
| `video.mode` | `always` |
| `spend.currency` | `USD` |
| `gui_worker_warn_threshold` | `2` |
| `app.maturity` | `shipped` |
| `app.platform` | `macos` |
| `budgets.on_budget` / `spend.overrun` | `drain` |
| `fail_on` | `scripted` |
| `pr.max_iterations` | `3` |
| `pr.max_wall_time` | `1h` (3600 seconds) |
| `pr.max_pr_updates` | `5` |
| `explorer.max_steps` / `max_time_s` | `40` / `120` |
| `explorer.decision.mode` | `heuristic` |
| `explorer.friction.enabled` / `fail_ci` | `true` / `false` |
| `visual.threshold` | `0.01` |
| `visual.judgment.enabled` | `false` |
| `visual.judgment.provider` | `command` |

`spend.max_spend` stays unset until the file or `--max-spend` sets it. The template comment suggests `10` USD when you turn on `backend = cloud`. Set that cap yourself. A cloud campaign without a positive cap fails fast.

## TOML shape

Top-level `intents` is an array of strings. Each entry is a markdown intent file, a JSON recorded flow, or a directory of those files. Paths are stored as written, whether they point at a file or a directory. The queue builder is what checks that those paths exist.

```toml
intents = ["intents/smoke.md", "intents/"]

[app]
path = "/path/to/MyApp.app"
build_command = ""
bundle_id = ""
launch_args = []
maturity = "shipped" # prototype | shipped
platform = "macos" # macos | ios
simulator = "iPhone 17" # name or UDID; used when platform = ios
simulators = ["iPhone 17", "iPhone 17 Pro"] # one per concurrent iOS worker

[app.env]
FEATURE = "1"

[campaign]
backend = "local" # local | vm | cloud
workers = 2
shard_strategy = "intent" # intent | suite | exploratory_seed
max_wall_time = "2h"
max_worker_minutes = 60
on_budget = "drain" # drain | cancel
fail_on = "scripted" # scripted | any | never
report_root = "reports"
gui_worker_warn_threshold = 2

[driver]
kind = "auto" # auto | fake | legacy | runner

[llm]
enabled = false
provider = "anthropic" # anthropic | openai | fake

[local]
isolation = "thread" # thread | subprocess

[vm]
provider = "tart"
image = "ghcr.io/cirruslabs/macos-sonoma-base:latest"
tart_bin = "tart"
recycle = true
cost_per_worker_minute = 0.0

[cloud]
adapter = "simulator"
cost_per_worker_minute = 0.05
estimated_shard_minutes = 1.0
endpoint_env = "AQA_CLOUD_ENDPOINT"
token_env = "AQA_CLOUD_TOKEN"

[spend]
max_spend = 10.0
currency = "USD"
overrun = "drain" # drain | cancel

[video]
mode = "always" # always | on_failure | exploratory_only

[pr]
mode = "off" # off | human | autonomous
max_iterations = 3
max_wall_time = "1h"
max_pr_updates = 5
fix_command = ""

[issues]
github = false
linear = false
template = "templates/issue.md"
github_repo = "owner/name"
github_token_env = "GH_TOKEN"
linear_api_key_env = "LINEAR_API_KEY"
linear_team = "TEAMID"

[visual]
enabled = false
baseline_dir = "baselines"
threshold = 0.01

[visual.judgment]
enabled = false
provider = "command" # command | fake. `mode` is accepted as an alias.
command_env = "AQA_VISUAL_JUDGE_COMMAND"
# command = ""  # used when the env var is unset
# judgment = ""  # canned text for provider = "fake"; empty or "fine" means the screen is fine
timeout_s = 60

[coverage]
scripted = true
exploratory = true
visual = false

[explorer]
engine = "legacy" # legacy | agent
max_steps = 40
max_time_s = 120
on_step_failure = "stop" # stop | continue

[explorer.decision]
mode = "heuristic" # heuristic | system_one | computer_use | cascade
escalate_after = 3
max_model_calls = 8
model_timeout_s = 30.0
cache_observations = true

[explorer.decision.system_one]
provider = "http" # http | fake
endpoint_env = "AQA_SYSTEM_ONE_ENDPOINT"
api_key_env = "AQA_SYSTEM_ONE_API_KEY"
min_confidence = 0.55
include_tree_depth = 4

[explorer.decision.computer_use]
provider = "command" # command | fake
command_env = "AQA_COMPUTER_USE_COMMAND"
max_calls = 3
include_a11y_hint = true

[explorer.friction]
enabled = true
emit_threshold = 50
min_extra_steps = 3
min_backtrack_rate = 0.15
personas = ["expert", "first_time"]
fail_ci = false
compare_to = "gold" # gold | prior_p50
klm = true
# Optional: [explorer.friction.allow_step_ratio] intent_id = 3.5

[suite]
command = "xcodebuild test -scheme MyApp"
```

`explorer.decision.mode` defaults to `heuristic`. Computer-use (`provider = "command"`) reads the shell command from the env var named by `command_env`, passes the screenshot path as argv, optional a11y hint JSON on stdin, and expects one JSON action on stdout. Use `provider = "fake"` in CI so no live command runs. Call caps are `computer_use.max_calls` and shared `max_model_calls`; timeout is `model_timeout_s`. See `docs/decision.md`.

`explorer.friction` meters advisory `friction_path` findings (gold-relative). `fail_ci` is reserved and not wired to `fail_on` yet. See `docs/friction.md`.

Unknown keys are ignored. Credentials are environment variable names (`endpoint_env`, `token_env`, `github_token_env`, `linear_api_key_env`).

### Where keys land

| TOML | `CampaignConfig` |
| --- | --- |
| `intents` | `intents` |
| `[app]` | `app` (`AppTarget`) |
| `campaign.backend`, `workers`, `shard_strategy`, `fail_on`, `report_root`, `gui_worker_warn_threshold` | the same fields |
| `campaign.max_wall_time` | `budgets.max_wall_time_s` |
| `campaign.max_worker_minutes`, `campaign.on_budget` | `budgets.max_worker_minutes`, `budgets.on_budget` |
| `[checks]` | `checks.settings`, read by `swarmqa.checks.config.checks_settings` |
| `[llm]` | `llm.enabled`; every other key goes to `llm.settings` and is read by `swarmqa.config.llm_settings` |
| `[driver]`, `[local]`, `[vm]`, `[cloud]`, `[spend]`, `[video]`, `[pr]`, `[issues]`, `[visual]`, `[coverage]`, `[explorer]`, `[suite]` | the matching nested config |
| `[visual.judgment]` | `visual.judgment` (`VisualJudgmentConfig`) |
| `[explorer.decision]`, `[explorer.decision.system_one]`, `[explorer.decision.computer_use]` | `explorer.decision` (`DecisionConfig` and nested provider configs) |
| `[explorer.friction]` | `explorer.friction` (`FrictionConfig`) |

`max_wall_time` strings go through `swarmqa.util.parse_duration`. Accepted forms are `90s`, `5m`, `2h`, `1h30m`, and `1h2m3s`. Units are required. Campaign wall time is optional. The PR loop wall time defaults to one hour.

### Maturity

`app.maturity` is `prototype` or `shipped` (the default). Prototype is a hint that exploration may wander and that failures are advisory. Shipped is the stricter pass/fail posture for scripted intents. The explorer reads this hint when it decides how aggressive to be. The config loader only checks that the value is one of those two names.

### Spend cap

`spend.max_spend` is the currency ceiling for a campaign. `spend.currency` is a non-empty code such as `USD`.

- `backend = cloud` requires `max_spend` to be present and greater than 0. Missing, zero, and negative caps fail fast with `spend.max_spend: required and must be > 0 when backend is cloud`.
- `backend = local` may set `max_spend`. The number stays on the config. The local runner keeps scheduling, and the campaign report notes `max_spend is ignored on the local backend`.
- `backend = vm` may set `max_spend`. It is enforced when `vm.cost_per_worker_minute` is non-zero. A zero VM rate with no cap is unmetered.

`overrun` and `on_budget` are `drain` (let the current shards finish) or `cancel` (stop them). `drain` is the default.

Several GUI sessions on one display get flaky. `gui_worker_warn_threshold` (default `2`) is the local worker count above which the orchestrator warns and points you at `vm` or `cloud`, where each worker has its own machine.

`driver.kind` picks the session driver. `auto` (default) uses `legacy` on a Mac or when `app.platform = "ios"`, and `fake` elsewhere. `fake` is a dry run that needs no Accessibility permission. `legacy` is the AppleScript macOS driver or the simctl/idb iOS driver, chosen by `app.platform`. `runner` is reserved for the XCUITest runner and is rejected until it ships.

`pr.mode` defaults to `off`: the campaign writes its report and no branch or PR is made. Set `human` or `autonomous` to opt into the fix loop.

### Explorer engine and checks

`explorer.engine` picks the exploratory explorer. `legacy` (the default) is the original one. `agent` runs the observe-decide-act loop in `docs/explorer-v2.md` with the checks from `[checks]`, plus the model from `[llm]` when `llm.enabled = true`; without a model it uses the free heuristic and breadth-first crawling. A model provider that cannot be built (for example a missing `swarmqa[anthropic]` extra) ends the shard as an error before the app launches.

`[checks]` takes `functional`, `layout`, `baseline` and `judge`. Each is on unless set to `false`, and a table overrides that check's settings (see `docs/checks.md`); an unknown check or setting fails validation. `layout.platform` defaults to `app.platform`. `baseline` defaults to on only when `visual.enabled`, using `visual.baseline_dir` and `visual.threshold`. When the model judge has no provider, `visual.judgment` (the command judge) is the fallback. When `explorer.friction.enabled` and the shard has a goal, the friction check also runs and reports once at the end of the session.

With the agent engine and `[llm]` enabled, model spend counts toward `spend.max_spend` on every backend, including `local`.

### Evidence

Every campaign runs the evidence pipeline (`docs/evidence.md`) and writes `findings.json` (schema v2). Set `app.source_dir` to the app's source checkout to add suspected source files to each finding and to keep the cross-run store in `<source_dir>/.aqa/state/findings.json`; only a complete run, with no stopped, errored, resumed or `--intent`-filtered shards, marks missing findings as fixed.

`aqa replay reports/<campaign>/findings/<id>.replay.json` replays one finding. It exits 1 when the finding still reproduces, 0 when it does not, and 2 when the replay could not run (for example the app did not launch).

### Models

`[llm]` is off by default, so no campaign calls a model or spends money until you set `enabled = true`. The other keys are `swarmqa.llm.settings.LLMSettings` fields (`provider`, `step_model`, `judge_model`, `max_cost`, `prices`, and so on); an unknown key or provider fails validation. See `docs/llm.md`.

### Video, coverage, issues

`video.mode` is `always` (default), `on_failure`, or `exploratory_only`.

`coverage.scripted`, `coverage.exploratory`, and `coverage.visual` toggle those passes. Visual comparison uses `visual.baseline_dir` and `visual.threshold` (0 through 1 inclusive).

`visual.judgment` is separate from that pixel compare. It stays off until `visual.judgment.enabled` is true, so existing campaigns do not call out. `provider = "command"` runs the command in `AQA_VISUAL_JUDGE_COMMAND` (or `command` when that variable is unset). Claude Code or Codex can be the command. It receives the PNG path as its last argument and must print one JSON object: `{"ok": true}` when the screen is fine, or `{"ok": false, "judgment": "what looks wrong"}` when it is not. A non-zero exit or unparseable output does not fail the campaign. `provider = "fake"` reads the canned `judgment` string and runs nothing. See `docs/visual.md`.

`issues.github` and `issues.linear` enable trackers. `issues.template` is the markdown ticket body. When it is unset, reporters use `swarmqa/templates/issue.md`. Tokens stay in the environment variables named by the config.

## CLI overrides

Flags on `aqa run` replace the file for that invocation. Omitted flags leave the file value in place. Repeated `--intent` replaces the whole `intents` array.

| Flag | Replaces |
| --- | --- |
| `--app` | `app.path` |
| `--intent` (repeatable) | `intents` |
| `--backend` | `backend` (`local`, `vm`, `cloud`) |
| `--workers` | `workers` |
| `--max-wall-time` | `budgets.max_wall_time_s` |
| `--max-spend` | `spend.max_spend` |
| `--spend-currency` | `spend.currency` |
| `--video-mode` | `video.mode` |
| `--pr-mode` | `pr.mode` |

```bash
aqa run --config aqa.config.toml --app /path/to/MyApp.app --intent intents/smoke.md --intent intents/
aqa run --backend cloud --workers 12 --max-wall-time 2h --max-spend 10 --spend-currency USD --video-mode on_failure --pr-mode human
```

An agent can invoke the same command. Raise `--workers` and `--max-spend` to test more. Lower `--max-spend` to stay inside a currency ceiling. `--backend vm` or `--backend cloud` isolates GUI sessions.

## Validation

Every message starts with its field path. `load_config` collects them and raises one `ConfigError`.

| Check | Message prefix |
| --- | --- |
| `backend` is `local`, `vm`, or `cloud` | `backend:` |
| `workers` is an integer `>= 1` | `workers:` |
| `video.mode` is `always`, `on_failure`, or `exploratory_only` | `video.mode:` |
| `pr.mode` is `human` or `autonomous` | `pr.mode:` |
| `app.maturity` is `prototype` or `shipped` | `app.maturity:` |
| `app.platform` is `macos` or `ios` | `app.platform:` |
| `visual.threshold` is from 0 to 1 inclusive | `visual.threshold:` |
| `visual.judgment.provider` is `command` or `fake`; `timeout_s` is `> 0` | `visual.judgment.provider:`, `visual.judgment.timeout_s:` |
| `spend.currency` is a non-empty string | `spend.currency:` |
| cloud `spend.max_spend` is present and `> 0` | `spend.max_spend:` |
| `pr.max_iterations` and `pr.max_pr_updates` are integers `>= 1` | `pr.max_iterations:`, `pr.max_pr_updates:` |
| `explorer.max_steps` is an integer `>= 1` and `explorer.max_time_s` is `> 0` | `explorer.max_steps:`, `explorer.max_time_s:` |
| `explorer.decision.mode` is `heuristic`, `system_one`, `computer_use`, or `cascade` | `explorer.decision.mode:` |
| `explorer.decision.escalate_after` `>= 1`, `max_model_calls` `>= 0`, `model_timeout_s` `> 0` | `explorer.decision.escalate_after:`, … |
| `system_one.provider` is `http` or `fake`; `min_confidence` in 0..1; `include_tree_depth` `>= 1` | `explorer.decision.system_one.*:` |
| `computer_use.provider` is `command` or `fake`; `max_calls` `>= 0` | `explorer.decision.computer_use.*:` |
| `explorer.friction.compare_to` is `gold` or `prior_p50`; `emit_threshold` 0..100 | `explorer.friction.*:` |

The same style covers the other closed sets: `shard_strategy`, `budgets.on_budget`, `fail_on`, `local.isolation`, `spend.overrun`, and `explorer.on_step_failure`. Unknown names are field errors, including an unknown backend, video mode, PR mode, platform, or decision mode. `app.simulators` is an array of non-empty strings. `app.simulator` is an optional string.

A missing file, including a path that is a directory, raises `ConfigError(["config: not found"])`. Unreadable bytes and TOML syntax errors raise `ConfigError(["config: invalid toml"])`. A bad duration raises a field error on `budgets.max_wall_time_s` or `pr.max_wall_time_s` and includes the `parse_duration` reason. `90s` and `2h` load. `30` and `soon` fail. A table written as a scalar (`campaign = "local"`) is `campaign: must be a table`.

`max_spend` on `backend = local` is valid. The loaded config still has that number.
