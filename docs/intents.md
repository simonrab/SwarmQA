# Intents

`build_queue` turns a campaign config into an ordered list of shards. A campaign can mix three sources:

- markdown intents (`.md`)
- version-1 JSON flows (`.json`, schema in `docs/flow.schema.json`)
- one suite command from `config.suite.command`

Hand-authored JSON loads through `build_queue` on its own. `record_flow` is only a writer.

## Paths

`config.intents` is a list of files and directories. A missing path raises `IntentError` (`intent not found: ...`). Directories are scanned recursively for `.md` and `.json` files and sorted by relative path. Other files are ignored. A file that is not markdown or JSON raises `IntentError`.

## Markdown

The first `#` heading is the shard name. Body prose is the text outside `## Constraints` and `## Steps`. The shard goal is the title and body separated by a blank line. When the file has no title, the file stem is the name.

`## Constraints` bullets become `shard.constraints` (the leading `-` or `*` is removed).

`## Steps` uses one action per bullet:

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

A role word is optional on `click`, `type` (`into <role> "Label"`), `wait`, and `assert`. `key` splits on `+` into `keys`. `menu` splits on `>` into `path`. `wait` durations use `parse_duration` (`5s`, `1m`, `1h30m`). `assert "Label" missing` (also `absent` or `not exists`) sets `exists` to false. `launch` is accepted so a recorded launch step can be replayed.

When `## Steps` contains at least one action, the shard is `kind="scripted"` and those actions are filled. An unrecognized step line raises `IntentError`. A markdown file with no step list is `kind="exploratory"` and carries the goal prose.

When `coverage.exploratory` is true and the markdown shard is scripted, the queue also gets an exploratory shard with the same name, goal, and constraints, and `seed="explore"`. That companion is markdown-only.

`examples/intents/smoke.md` is a scripted markdown intent.

### Tags

Each shard gets `tags`, lower-cased with inner spaces turned into `-` and duplicates dropped, in this order: `tags:` in YAML front matter (`---` block at the top), items under a `## Tags` section, then `flow:<slug>`. The exploratory companion of a scripted intent also gets `gold_steps:<n>` (the scripted step count) unless a gold tag is already declared. Front matter and the Tags section are not part of the goal. See `docs/checks.md`.

## JSON flows

A `.json` intent is one `kind="scripted"` shard. `version` must be the integer `1`. Any other version, including a missing version, raises `IntentError` (`flow version must be 1`). The shard name is the flow `name`. Each step becomes an `Action`. Unknown document, step, or target fields are rejected. The shard actions are the steps in order.

`examples/intents/smoke.json` is a hand-authored flow. It does not need `record_flow`.

## Suite and visual shards

When `config.suite.command` is set, the queue gains one `kind="suite"` shard named `suite` whose `suite_command` is that command.

When `config.visual.enabled` or `coverage.visual` is true, the queue gains one `kind="visual"` shard named `visual`. `visual_names` is the sorted stems of `*.png` files directly in `visual.baseline_dir` when that directory exists. A missing baseline directory leaves `visual_names` empty. Enabling both flags still produces a single visual shard.

Suite and visual shards are appended after intent shards, suite first.

## Coverage

- `coverage.scripted = false` drops `kind="scripted"` shards, including JSON flows. A scripted markdown file still contributes its exploratory companion when exploratory coverage is on.
- `coverage.exploratory = false` drops `kind="exploratory"` shards, including the `seed="explore"` companion.
- Suite shards stay when scripted or exploratory coverage is off. The visual shard is added when visual coverage or `visual.enabled` asks for it.

## Order and ids

`shard_strategy` reorders the queue after shards are collected:

| Strategy | Order |
| --- | --- |
| `intent` | Intent file order, then the suite shard, then the visual shard |
| `suite` | Suite shards first; everything else keeps its relative order |
| `exploratory_seed` | Exploratory shards first; everything else keeps its relative order |

An unknown strategy raises `IntentError`.

Ids are assigned after ordering: `s-<n>-<slug>` with `n` starting at 1 and `<slug>` from `swarmqa.util.slug` applied to the shard name. A scripted markdown shard and its exploratory companion share a slug and differ by `n`.

## Empty intent set

If coverage, suite, and visual settings leave no shards, `build_queue` raises `IntentError` with the message `intent set is empty`. An empty directory, or a directory that only contains non-intent files, is empty when suite and visual shards are not added. A suite command or a visual shard alone is a valid queue.

## Recording

`record_flow(*, app_path, out_path, name="recorded-flow", interactive=False, events=None) -> Path` writes pretty JSON (`indent=2`, trailing newline) matching the schema:

```json
{
  "version": 1,
  "name": "recorded-flow",
  "steps": [
    {"action": "launch"}
  ]
}
```

- `events` is a list of action objects. Those steps are validated and written. This is the path tests and agents use without a display.
- `interactive=True` reads stdin lines with the step grammar and no leading hyphen. Blank lines are skipped. Reading stops on a line `done` or on EOF.
- Otherwise the file contains a single `launch` step. `aqa record` prints the path this function returns.

`events` wins when it is not `None`. An empty `name` raises `IntentError`. Parent directories of `out_path` are created.
