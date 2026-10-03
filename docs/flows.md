# Flows

Flows are goal shards that SwarmQA puts in front of the configured intents. They come from two places: the code change under test (flows from the diff) and earlier explorations that reached their goal (the replay cache). Both read the `[flows]` table, parsed by `swarmqa.flows.settings.FlowsSettings`. The contract types (`Change`, `FlowPlan`, `FLOW_TAG`) live in `swarmqa.flows`.

## Flows from the diff

`swarmqa.flows.from_diff` turns a code change into exploratory goal shards, most important first.

```python
change = collect_change(repo_dir, base, head)          # -> Change
plan = flows_from_diff(config, change, provider=model)  # -> FlowPlan
```

### Collecting the change

`collect_change(repo_dir, base, head, *, max_chars=None, settings=None, runner=git_runner)`:

- runs `git diff base...head`, or `base..head` when the two have no merge base. Renames show up as a delete plus an add;
- cuts the diff to `max_chars`, or to `flows.max_diff_chars` when `max_chars` is not given, and sets `Change.truncated` when it does;
- reads every touched file that matches `flows.file_globs` at `head` (`git show head:path`), capped at `flows.max_file_chars`. A glob matches the repo-relative path or the file name, so `*.swift` and `Sources/*View.swift` both work. Deleted files are listed with empty content;
- raises `ChangeError` when git cannot diff the two revisions.

`runner` is the command seam (same shape as `swarmqa.devices.commands`). Tests pass a fake that returns recorded output.

### Proposing flows

`flows_from_diff(config, change, *, provider=None, max_flows=None, settings=None)` returns a `FlowPlan`:

| Case | `source` | Shards |
| --- | --- | --- |
| `flows.from_diff = false` | `none` | none |
| empty diff | `none` | none |
| provider answers | `model` | one per proposed flow (none if the model proposed none) |
| no provider, or the provider raised `ModelError` | `heuristic` | one per touched SwiftUI view |

`notes` says why a fallback was used, and also records a truncated diff. `usage` carries the model call's `Usage`. The provider meters its own spend, so nothing else needs recording.

With a provider, the diff and the touched files go to `propose_flows(diff, files, max_flows=...)`. Each `ProposedFlow` becomes a `Shard`:

- `kind="exploratory"`, `id=""` (`build_queue` assigns ids), `name` from the flow;
- `tags`: `diff`, `priority:<n>` (clamped to 1..5, default 3), then `flow:<slug>`, normalised like intent tags;
- `goal`: the flow's goal, then `Steps: ...` and `Expect: ...` from its hints.

Shards are sorted by priority, then by the model's order. Flows whose normalised name or goal repeats an earlier one are dropped, and the list is capped at `max_flows` (default `flows.max_flows`).

### Goal phrasing

The agent loop reads a goal with `parse_goal`: double-quoted phrases are the controls that mark the goal done, and every other significant word is a search token. So the goal is written as follows:

- quotes in the goal sentence and in the expected outcomes are kept, so `The "Saved" banner shows` ends the flow once **Saved** is on screen;
- quotes in the step hints are dropped, so their labels steer the search without all having to be on one screen. If nothing in the goal or the expected outcomes is quoted, the last quoted step hint keeps its quotes;
- single-quoted labels become double-quoted, curly quotes become straight quotes, and apostrophes become `’`, so `user's` is not read as a quote.

Example:

```text
Turn on dark mode from Settings. Steps: Tap Settings; Flip the Dark Mode toggle. Expect: The "Dark Mode" switch is on.
```

### Heuristic fallback

Without a model, `heuristic_flows(change)` reads the Swift files in the diff:

- views are `struct X: View` declarations, found in the file at `head` and in the diff's own lines;
- each added or removed line belongs to the view declared (or extended) above it. When the head content is unknown and no declaration comes before the line, it belongs to the file's first view;
- controls come from added lines only: `.accessibilityIdentifier("x")`, and the string labels of `Button`, `NavigationLink` and `Toggle`. A control outside any view is filed under the file name.

Each touched view gives one shard, named `<View> view`, with the goal `Open <View> and use "<control>", ...` (up to six controls). `View` is dropped from the struct name and camel case is split, so `ProfileEditorView` reads **Profile Editor**. Views with new controls get priority 2, the others priority 3. Swift files with no view give no shard. A change with no views gives an empty plan with a note.

## Replay cache and self-heal

**Off by default.** A cached flow runs as a scripted replay, and scripted shards run none of the agent-loop checks (layout, dead taps, hangs, the judge). Once a path is cached, a clipped label or a dead button on it is no longer reported; only missing elements and crashes are. Set `cache = true` when speed matters more than those checks on known-good paths.

An exploration that reached its goal is a known path through the app. The
replay cache (`swarmqa/flows/cache.py`) keeps that path and runs it as a
script the next time the same goal comes up, so a cached run costs one
scripted replay and no model calls. When a cached step no longer matches the
app, only that step is explored again (`swarmqa/flows/heal.py`) and the
script is rewritten.

```toml
[flows]
cache = false             # true replays cached flows instead of exploring
cache_dir = ".aqa/flows"  # relative to app.source_dir, else the config's dir, else cwd
heal = true               # re-explore a failing cached step and rewrite the script
heal_max_steps = 8        # agent-loop steps allowed to repair one step
```

### What is stored

Each entry is two files under the cache directory, named by the shard's key:

- `<key>.json` is a version-1 JSON flow (`version`, `name`, `steps`), the
  same format as any flow in `intents`, so `aqa replay` and the intent
  loader read it unchanged. The session's opening `launch` is dropped,
  since the scripted runner launches the app itself.
- `<key>.meta.json` holds the cache's bookkeeping: the goal, `created` and
  `updated` timestamps, `hits` (passing replays), `heals` (times a step was
  repaired) and the source `sha` when known. The flow format rejects extra
  fields, so this lives beside the flow.

The key (`shard_key`) is a slug of the shard's name plus a 12-character hash
of its name and goal, lower-cased with runs of whitespace collapsed. A flow
proposed again from a later diff, with the same name and goal, hits the same
entry.

Commit the directory if you want CI runs to share it, or ignore it to keep
the cache per checkout. Do not list it under `intents`: the `.meta.json`
files are not flows.

### How a run uses it

1. **Recording.** After every agent-loop session the worker's
   `session.json` goes to `FlowCache.record_session(shard, path)`. It is
   saved only when `goal_reached` is true: goal mode, stopped on `done`, no
   findings, no crash. A session that gave up, hit a budget, or found a bug
   never becomes a cached path.
2. **Serving.** `cached_shards(config, shards)` swaps every exploratory
   shard with a goal that has an entry for a scripted shard: same id, name
   and goal, its tags plus `cached`, and the cached steps as its actions.
   Misses and every other shard pass through unchanged. With
   `cache = false` nothing is swapped.
3. **Replaying.** The scripted shard runs like any other. A pass counts as a
   hit.
4. **Healing.** When a cached step fails because its element is not on
   screen (a renamed button, a changed identifier), the scripted runner asks
   the `Healer` hook before it records a finding. The hook runs the agent
   loop in goal mode from the screen the app is on, with no checks and at
   most `heal_max_steps` steps. The model sees the original goal plus a hint
   naming the failed step. The heuristic sees the goal alone, since extra
   words would change what it looks for. If the loop reaches the goal, its
   steps replace the failed step and everything after it, the entry is
   saved with `heals + 1`, and the shard passes with the step marked
   `healed`. If it does not, the entry is dropped, so the next run explores
   from scratch, and the missing-control finding stands.

A crash, a timeout, or a failed launch during a cached replay is reported
as a finding and is never healed, and the entry stays. Those are bugs in
the app, not a stale script. With `heal = false`, a failing cached step is
reported like any scripted failure and the entry is kept.
