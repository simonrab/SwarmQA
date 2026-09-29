# Explorer v2: the agent loop

`swarmqa/explorer/agent_loop.py` runs one exploratory shard as an observe-decide-act loop. `swarmqa/explorer/screen_graph.py` holds the screen fingerprints and the screen graph the loop builds. Work package WP-B2 in `docs/build-plan.md`.

```python
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop

result = run_agent_loop(
    shard, driver, config,
    provider=provider,          # a ModelProvider, or None
    checks=checks,              # swarmqa.checks.protocol.Check objects
    settings=AgentLoopSettings.from_config(config, mode="crawl"),
    work_dir=work_dir, worker_id="w1", backend="local",
)
```

The caller owns the driver and closes it. Any v1 driver works: the loop wraps it with `as_v2`.

## One step

1. `observe()` a fresh tree (and screenshot). The loop never reuses an earlier tree.
2. Fingerprint the tree and add the screen to the graph.
3. Build a `StepContext` and run the checks. Checks with `per_screen = True` run only on a screen's first visit. Per-step checks run every step.
4. Decide the next `StepDecision`.
5. Do it on the driver. Then go back to step 1.

The first screen gets a context with `before=None` and `action=None`. A check that raises is logged as a failed step named `check <name>` and skipped.

## Settings (`AgentLoopSettings`)

| Field | Default | Meaning |
|---|---|---|
| `mode` | `goal` | `goal` follows `shard.goal`, or crawls when the goal is empty. `crawl` always crawls. |
| `max_steps` | 40 | Driver actions (taps, types, swipes, keys, backs, relaunches). Observations are free. |
| `max_wall_time_s` | 300 | Measured with the injected `clock`. |
| `max_model_calls` | 20 | Calls to `decide_step`, failed ones included. |
| `model_timeout_s` | 30 | Passed to `decide_step`. |
| `stall_limit` | 5 | Goal mode ends after this many steps in a row that failed or left the screen unchanged; crawl mode after this many failed actions in a row; both after this many timed-out observations in a row. |
| `per_screen_checks` | true | Off skips the per-screen (model) checks entirely. |
| `screenshot_every_step` | true | Off captures a screenshot only on a screen's first visit. |
| `heuristic_first` | true | A confident heuristic pick skips the model. |
| `history_limit` | 20 | How many earlier steps the model sees. |
| `sample_text` | `test` | What crawl mode types into text fields. |
| `max_crash_relaunches` | 3 | The session stops after one more crash than this. |
| `report_unchecked_crashes` | true | Add a crash finding when no check reported one. |
| `platform` | `config.app.platform` | Picks the back-navigation rule. |

`AgentLoopSettings.from_config(config, **overrides)` takes `max_steps`, `max_time_s`, `decision.max_model_calls` and `decision.model_timeout_s` from `config.explorer`, and the platform from `config.app`.

## Screen fingerprints

A fingerprint is the first 12 hex characters of the SHA-256 of the screen's structural skeleton. The skeleton keeps:

- the role of every element that matters: interactive elements, elements with an identifier, title containers (`navigationbar`, `toolbar`, `tabbar`, `sheet`, `alert`, `dialog`), and containers holding any of these;
- every identifier, with digit runs folded to `#`;
- the labels of interactive elements and title containers, lower-cased, with digit runs folded to `#`.

It leaves out values, frames, `enabled`, static text, and images without an identifier. Siblings that are identical after masking count once, so a list with 2 or 20 look-alike rows (for example identifiers `item-17`, `item-18`, …) is the same screen.

The same screen with different field contents, times or counters keeps its fingerprint. An alert, a new button, or a different navigation title makes a new one.

Known limits: two screens with the same controls and only different static text (for example two detail pages) share a fingerprint. List rows whose labels differ are different elements, so a list whose row labels change counts as a different screen.

## Screen graph

Nodes are screens: the fingerprint, the first screenshot, the actionable controls (enabled interactive elements with a label or identifier, keyed `role|label|identifier`), and which controls have been tried. Edges are `(screen, action) -> screen`. The action is plain JSON (`{"kind": "tap", "target": {...}}`), and the latest observed target wins.

- `untried_frontier()` lists screens with untried controls, breadth-first from the start screen.
- `path_to(screen)` is the shortest known edge path from the start screen, used for navigation and repro.
- `nearest_frontier(screen)` is the closest screen with untried controls reachable from the current one.
- `coverage()` counts screens, edges, controls, tried controls and the frontier.
- `save`, `load`, `to_dict`, `from_dict`, and `merge(other)` combine graphs from several devices: controls and tried sets are unioned, visits and edge counts summed, and the first screenshot kept.

Self-loops (an action that left the screen unchanged) are stored but never used as routes. When a step that follows a known edge fails, the edge is dropped. This holds in both modes, including goal mode's crawl fallback: the plan carries a navigation flag, so the check does not depend on the step's source label.

The loop saves the graph to `<work_dir>/screen_graph.json`.

## Crawl mode

No model is used. On each step:

1. Tap (or type `sample_text` into) the first untried control on the current screen, in tree order. A control recorded for this screen but no longer in the tree (for example "Delete 3 items" that became "Delete 2 items" under the same fingerprint) is marked tried and skipped. A control is marked tried when it is attempted, whether or not the action works.
2. Otherwise follow the first edge of the path to the nearest screen with untried controls.
3. Otherwise, once per screen, try `back`.
4. Otherwise relaunch and follow `path_to` from the start screen. A screen that still cannot be reached after one relaunch is marked unreachable and skipped.
5. Stop when no reachable screen has untried controls (`frontier_empty`), after `stall_limit` failed actions in a row (`stall`), or when the budget runs out.

## Goal mode

The heuristic runs first because it costs nothing:

- With a provider and `heuristic_first`, the heuristic acts only when exactly one untried tappable control on the screen matches a name the goal expects (`decision/heuristic.py` `parse_goal` and `matches_name`). Otherwise the model decides with `decide_step(obs, goal, history)`.
- Without a provider, the heuristic taps the first untried control sharing a goal word, says `done` once the expected controls are present and none is left, and otherwise crawls.
- When the model errors (`ModelError`), returns an unusable decision (for example a tap with no target), or the call cap is reached, the step falls back: an untried control sharing a goal word, else the crawl choice, else `give_up`. Model errors are logged as failed `model` steps. They never end the session.

The loop stops on `done`, `give_up`, a stall, or the budget.

## Actions

| Kind | Driver call | Replay step |
|---|---|---|
| `tap` | `click(target)` | `click` |
| `type` | `type_text(target, text)` | `type` |
| `tap_point` | `tap_point(x, y)` | `tap_point` with `point` |
| `swipe` | `swipe(start, end)` | `swipe` with `point` and `end` |
| `key` | `keychord(keys)` | `key` |
| `back` | see below | what was done |
| `done`, `give_up` | none | none |

Driver queries match labels by substring and act on the first match, so a query for "Save" also hits an earlier "Save As". Before a `tap` or `type`, the loop finds the element the query means, preferring an exact label match. If the driver's first match is a different element, the loop adds the element's identifier to the query when that makes it unique. Otherwise it taps the centre of the element's frame (tap only). Tried controls are marked by exact identity (`role|label|identifier`). The replay records exactly what the driver ran: the narrowed query, or a `tap_point` at that centre. A replay then reaches the same element.

`UnsupportedAction` and `ElementNotFoundError` are recorded as failed steps and left out of the replay. `UITimeoutError` is a failed step but stays in the replay, so the replay shows the unresponsive control.

### Back rule

On both platforms, first tap a visible, enabled tappable element whose label or identifier is `back`, `done`, `close` or `cancel`, preferring one inside a navigation bar. Otherwise:

- macOS: press `escape`.
- iOS: swipe in from the left edge, from `(1, h/2)` to `(0.7 w, h/2)`. The replay records it as a `swipe` step. A v1-only driver cannot swipe horizontally, so there the step fails.

## Timeouts

If the observation after an action raises `UITimeoutError`, the step is recorded and a failed `observe` step is added. The checks then run with `ctx.error` starting `timeout: observe:` and an empty `after` tree, so a functional check can report a hang. The next turn observes again before deciding anything. When that works, the edge from the action is recorded and the loop carries on. After `stall_limit` timed-out observations in a row, the session stops with `unresponsive`.

With `screenshot_every_step` off, a new screen's screenshot is taken with `driver.screenshot()` after the observation. The tree, and so the fingerprint, stays the one observed. A crash or timeout while taking it counts against the step that led there.

## Crashes

When the driver raises `AppCrashedError` during an action, on the observation after it, or while taking a new screen's screenshot:

1. The checks run with `ctx.crashed = True`, `before` set, and an empty `after` tree.
2. If no check returned a `crash` issue and `report_unchecked_crashes` is on, the loop adds `Crash on <control>`.
3. The loop relaunches (one step) and carries on while the budget and `max_crash_relaunches` allow. The control that crashed stays tried, so crawl mode does not tap it again.

A failed launch returns status `error` with a `launch` finding (or `Crash on launch`).

## Findings and replays

Check issues become findings only through `CheckIssue.to_finding`. Issues are deduplicated within the run by `fingerprint_for(kind, title, target)`, the same fingerprint `to_finding` sets. When an issue has no screenshot, the loop attaches the step's screenshot.

Each finding's `steps` is the human-readable session so far, starting with `launch`. Its replay is the matching version-1 flow (`launch`, `click`, `type`, `key`, `tap_point`, `swipe`, `relaunch`), written with `reporter.findings.write_replay` to `<campaign>/findings/<id>.replay.json`. `intent.ingest` loads it and `explorer/scripted.py` runs it. The replay covers the whole session, relaunches included, so state the app keeps across launches is reproduced too. The loop also writes `findings/<id>.md` with `write_finding`.

Media paths are relative to the campaign directory when `work_dir` is `<campaign>/workers/<id>`, and absolute otherwise.

The video follows `video_policy`, like the v1 explorer. The shard's status is `failed` if any finding is not advisory, `error` on a launch failure or an internal error, and `passed` otherwise. The last step result, `stop`, records why the session ended (`done`, `give_up`, `stall`, `frontier_empty`, `max_steps`, `max_wall_time`, `too_many_crashes`, `relaunch_failed`, `unresponsive`). `estimated_cost` sums `usage.cost` from the model calls.
