# Flows from the diff and the replay cache

Two Wave D pieces make campaigns follow the change and run faster on repeat:

- **Flows from the diff** (`swarmqa/flows/from_diff.py`): a model reads the change and writes intents for the user flows it puts at risk.
- **Replay cache** (`swarmqa/flows/cache.py`): when the agent loop reaches an intent's goal, the path is saved and replayed first next time, with no model calls. A step that no longer works is re-explored on its own and the saved path is updated.

## Flows from the diff

```bash
aqa flows propose --base main                 # HEAD vs main, in app.source_dir (or .)
aqa flows propose --base main --worktree      # include uncommitted changes
aqa flows propose --base origin/main --head feature --out intents/from-diff
aqa run --from-diff main                      # propose, then run them with the configured intents
```

Needs a model: `[llm] enabled = true`. What happens:

1. `git diff --name-only base...head` lists the changed files. Only files matching `flows.include` (Swift, strings, storyboards by default) and not `flows.exclude` (tests) count. If none changed, no model is called and nothing is written.
2. The diff of those files and the changed files' content (cut to `max_diff_bytes` and `max_file_bytes`, at most `max_files` files) go to `ModelProvider.propose_flows` with the prompt in `swarmqa/llm/prompts/flows.md`.
3. Flows are deduplicated by name, sorted by priority (1 is most at risk) and capped at `max_flows`.
4. Each becomes one markdown intent, `<rank>-p<priority>-<slug>.md`, so a directory of them loads most important first. The goal carries the model's hints (`## How to get there`), the expected outcomes and the changed files; there is no `## Steps` section, so each one runs as an exploratory shard. Tags: `from-diff`, `priority:<n>`. Writing again replaces earlier generated files and leaves hand-written ones alone.

`aqa run --from-diff BASE` writes them under `reports/diff-intents/local-<time>/` and adds that directory to the run's intents.

### On GitHub runs

With `[flows] from_diff = true`, `aqa watch` and `aqa run --github-sha` propose intents once per commit and add them to every platform's campaign. The diff is read from the builder's mirror (`~/.aqa/repos/<name>.git`): a PR compares its base branch with the head commit (the mirror's default branch when the base is unknown), a push compares the commit with its first parent. Intents go to `reports/diff-intents/<sha12>/`. If anything fails (no model, git error), the run logs it and goes on with the configured intents.

## Replay cache

Used by `explorer.engine = "agent"` for exploratory shards with a goal. Cached flows live in the app repo's `.aqa/flows/<platform>/<intent>-<goal hash>.json` (`app.source_dir`), or in `flows.cache_dir`. With neither set there is no cache, so nothing is written by surprise. `.aqa/` should stay out of version control.

A cached flow stores each step's action plus the fingerprint of the screen it starts on and the one it leads to, and the end screen.

On the next run of the same intent (same name and goal) on the same platform:

- The loop follows the cached steps in order whenever the current screen matches the step's start screen. No model is asked; the checks still run on every screen, so cached runs still find bugs.
- When every step has been used and the end screen is showing, the loop stops with `done`.
- **Self-heal.** A step whose target is gone, that fails, or that leads to a different screen is a miss. The loop explores from there with the heuristic and the model as usual. As soon as it reaches a screen a later cached step starts on, the replay picks up again. When the goal is reached, the new path replaces the old one and `heals` goes up.
- A crash during a cached step stops the replay for that run, so relaunching does not crash the same way again.
- A run that does not reach the goal counts a failure; after `flows.max_failures` in a row (default 3) the cached flow is deleted and the intent is explored from scratch next time.

The worker's step log says what happened, for example `cache: replayed 3/3 cached step(s); 0 re-explored`. `aqa flows list` shows every cached flow with its steps, runs, heals and failures.

## Config: `[flows]`

```toml
[flows]
from_diff = false        # on GitHub runs, add intents proposed from the change (needs [llm])
max_flows = 10
include = ["*.swift", "*.strings", "*.xcstrings", "*.storyboard", "*.xib"]
exclude = ["*Tests/*", "*Tests.swift"]
max_files = 20
max_file_bytes = 20000
max_diff_bytes = 60000
cache = true             # replay saved goal paths first
cache_dir = ""           # default: <app.source_dir>/.aqa/flows
max_failures = 3
```

Unknown keys are errors.
