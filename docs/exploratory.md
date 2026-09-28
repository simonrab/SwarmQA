# Exploratory hunting

`swarmqa.explorer.exploratory.run_exploratory` drives one exploratory shard against a single app session. Scripted steps stay in `run_scripted`. This module owns the loose-goal hunt: it launches the app, runs an observe→decide→act loop, and writes findings when the UI crashes, hangs, shows an error or empty state, or (on a prototype) is missing a control the goal named.

```python
run_exploratory(shard, driver, config, *, worker_id, work_dir, evaluator=None) -> WorkerResult
```

The driver is anything that matches `AppDriver`. Tests use `FakeDriver`. Pass an optional `evaluator=` to inject a `DecisionEvaluator` (see `swarmqa.decision`); otherwise `build_evaluator(config)` builds one from `explorer.decision`.

## Budgets

The hunt stops when `config.explorer.max_steps` or `config.explorer.max_time_s` is spent. It also stops early once the decision evaluator returns `done`/`noop` (default heuristic: every expected control from the goal is present and is not a menu that still needs to be opened).

Each of these costs one step:

- launch
- reading the accessibility tree
- each click
- each menu selection
- each screenshot taken for a finding

Recording start/stop does not cost a step. A step that would exceed either budget is not started. `WorkerResult.steps` never grows past `max_steps`. Time is wall time from the start of the hunt (`time.monotonic`).

`explorer.on_step_failure` applies to crash and timeout actions. `stop` (the default) ends the hunt. `continue` keeps going until the goal is satisfied or the budget ends. Observations such as `error_state` and `missing_control` do not use that switch.

## Observe → decide → act

After launch (and optional video start), the hunt:

1. **Observe** — read the accessibility tree once (one budget step) and scan for `error` / `empty` fault labels.
2. **Decide** — ask the `DecisionEvaluator` for the next `DecisionAction` given goal tokens, the tree, maturity, steps left, and which clicks/menus were already tried.
3. **Act** — perform that click or menu (each costs a step), emit `missing_control` when asked, or stop on `done`/`noop`.

Default `explorer.decision.mode = heuristic` keeps the classic strategy order below and is behavior-identical for existing FakeDriver tests. `system_one`, `computer_use`, and `cascade` currently fall back to the same heuristic (model backends land in later PRs).

## Strategies (heuristic)

Goal text is `shard.goal`, or `shard.name` when the goal is blank. Quoted phrases (`"Dark Mode"`) are the expected controls. Otherwise every significant word is an expected control. Short tokens and glue words (`open`, `click`, `the`, `button`, `menu`, …) are ignored, so `Open the Settings gear` expects **Settings** and **gear**.

The heuristic evaluator chooses actions in this order:

1. **Click** enabled `button` elements whose labels share a goal word, in tree order. Disabled buttons are left alone. A disabled control still counts as present.
2. **Menus** (only when expected controls are still unsatisfied). Open menu paths whose labels share a goal word (`File > Export` becomes `select_menu(["File", "Export"])`). For an expected name that is still nowhere in the tree, also try a one-item menu path of that name.
3. **Prototype gap.** When `app.maturity == "prototype"` and an expected control is still absent after the menu attempts, emit one `missing_control` finding. Shipped builds do not get that finding; a missing control is skipped. The finding is a report, not a silent return.
4. Otherwise **done**.

While observing, any element whose **label or value** contains `error` or `empty` (substring, case-insensitive) becomes an `error_state` finding. A blank value does not. The words have to appear in the text.

## Decision config

```toml
[explorer.decision]
mode = "heuristic"       # heuristic | system_one | computer_use | cascade
escalate_after = 3
max_model_calls = 8
model_timeout_s = 30.0
cache_observations = true
```

Nested `[explorer.decision.system_one]` and `[explorer.decision.computer_use]` hold provider settings for later backends. See `docs/config.md`.

## Findings

| Situation | Kind | Severity |
| --- | --- | --- |
| `AppCrashedError` on launch or during an action | `crash` | `critical` |
| `AppMissingError` (bundle missing or not launched) | `launch` | `critical` |
| `UITimeoutError` | `unresponsive` | `high` |
| Label or value contains `error` or `empty` | `error_state` | `medium` |
| Prototype goal control still absent | `missing_control` | `high` |

`WorkerResult.status` is `passed` when there are no findings, `failed` when there are, and `error` only when the driver raises something that is not one of those UI failures. The `error` string is that fault. Hypotheses are not copied there.

Each finding's `details` is a short hypothesis, for example `Settings gear missing — trying menu bar`, `Save crashed the app`, or `Inbox shows an empty state`.

Environment fields on the finding are `path`, `bundle_id`, and `version` from `driver.metadata()` after a successful launch.

## Evidence

Video follows `swarmqa.video_policy` and `config.video.mode` (`always`, `on_failure`, `exploratory_only`). The session records from the start when the policy says to, and the file is deleted when `keep_video` is false. A failure to start the recorder does not fail the shard.

Screenshots and the session video live under `work_dir` (`work_dir/media/`). When `work_dir` is `…/workers/<id>`, the campaign directory is the grandparent and paths stored on the finding are relative to it (`workers/<id>/media/…`, `findings/…`). Otherwise the work directory itself is the campaign directory.

For every finding the module writes:

- `findings/<id>.md` — title, severity, kind, steps, evidence paths, worker id, backend, fingerprint, and the hypothesis
- `findings/<id>.replay.json` — version-1 flow of the actions actually performed up to and including that finding (launch, clicks, menus, screenshots)

`fingerprint` is SHA-256 of `kind + newline + title.strip().lower() + newline + target.strip().lower()`, first 16 hex characters. The same filenames and hash are what `swarmqa.reporter.findings` will produce. Those helpers are called when they no longer raise `ChunkNotReady`. Until then this module writes the files itself and does not modify the reporter.

The session is left launched when the hunt returns so the caller can still inspect the driver. Video is stopped first.
