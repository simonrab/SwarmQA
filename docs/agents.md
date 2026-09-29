# Agent entrypoints

Autonomous QA is a local CLI. Cursor, Claude Code, and Codex run the same `aqa` commands a person would run in a shell. The tool does not pick a model vendor and it does not merge pull requests.

Install once, then point a campaign at a built app and an intent file or directory:

```bash
python -m pip install -e ".[dev]"
aqa init
```

## Campaign command

Cloud (spend cap required, sample ceiling 10 USD):

```bash
aqa run --app /path/to/MyApp.app \
  --intent intents/ \
  --backend cloud \
  --workers 12 \
  --max-spend 10 \
  --spend-currency USD \
  --pr-mode human
```

Local smoke (the locked default is `--backend local` and `--workers 2`). `--max-spend` is accepted and recorded; pure local runs do not stop on it:

```bash
aqa run --app /path/to/MyApp.app \
  --intent intents/smoke.md \
  --backend local \
  --workers 2 \
  --max-spend 10
```

| Flag | Meaning |
| --- | --- |
| `--backend` | `local`, `vm` (Tart), or `cloud` |
| `--workers` | Max concurrent shards. Default is 2 |
| `--max-spend` | Currency ceiling. Required and greater than 0 when `--backend cloud` |
| `--spend-currency` | Default `USD` |
| `--pr-mode` | `off` (default), `human`, or `autonomous` |
| `--max-wall-time` | Campaign scheduling budget (`90s`, `5m`, `2h`). This is not the PR-loop cap |

One campaign produces one merged report and at most one fix/PR loop. Workers do not open their own pull requests. The loop branch is `aqa/<campaign-id>`. The draft body is `reports/<campaign-id>/pr/draft.md` (title, failing shards, steps, video, screenshots, replay path).

## Safety caps

Autonomous mode repeats fix, then update the same draft PR, then retest only the shards that failed. All three caps apply together. The first one that trips wins. The pull request stays open, remaining finding ids are returned, and nothing merges to the default branch.

| Cap | Config | Default | `stop_reason` |
| --- | --- | --- | --- |
| Iterations | `pr.max_iterations` | 3 | `max_iterations` |
| Wall time | `pr.max_wall_time` | `1h` (`pr.max_wall_time_s`) | `max_wall_time` |
| PR updates | `pr.max_pr_updates` | 5 | `max_pr_updates` |

Other stop reasons: `human` after the default draft, `green` when a retest has no failed scripted shards, `no_fixer` when autonomous mode has neither a `fixer` nor `pr.fix_command` / `AQA_FIX_COMMAND`. Do not raise the caps in an agent session unless the user asks. Do not run `gh pr merge`.

Human mode writes the draft and, when `gh` is available, `gh pr create --draft`. It does not edit product code.

## Copy-paste prompts

### Cursor

```
You are running Autonomous QA in this repo. Use the terminal and do not merge any pull request.

aqa run --app /path/to/MyApp.app --intent intents/ --backend cloud --workers 12 --max-spend 10 --spend-currency USD --pr-mode human

One loop for the campaign, on branch aqa/<campaign-id>. Human mode writes reports/<campaign-id>/pr/draft.md and opens a single draft PR. It does not apply code fixes.

Safety caps — stop when any one trips and leave the draft PR open:
- pr.max_iterations = 3 (stop_reason max_iterations)
- pr.max_wall_time = 1h (stop_reason max_wall_time)
- pr.max_pr_updates = 5 (stop_reason max_pr_updates)

Never gh pr merge and never merge into the default branch. Use --pr-mode autonomous only if I ask, and keep these same caps.
```

### Claude Code

```
Use Bash to run Autonomous QA. Do not merge to the default branch and do not open one PR per worker.

aqa run --app /path/to/MyApp.app --intent intents/ --backend cloud --workers 12 --max-spend 10 --spend-currency USD --pr-mode human

The campaign may fan out across --workers on the chosen --backend. Cloud requires --max-spend (sample cap 10 USD). Local ignores the spend cap and still accepts the flag.

Honor the fix-loop safety caps in aqa.config.toml. They all apply; the first one that trips stops the loop and leaves the PR open:
- pr.max_iterations default 3, stop_reason max_iterations
- pr.max_wall_time default 1h, stop_reason max_wall_time
- pr.max_pr_updates default 5, stop_reason max_pr_updates

Retest only the shards that failed. Stop with stop_reason green when no scripted shard is still failing.
```

### Codex

```
Run this command in the repository to start Autonomous QA:

aqa run --app /path/to/MyApp.app --intent intents/ --backend vm --workers 4 --max-spend 10 --spend-currency USD --pr-mode autonomous

You may be the fixer via pr.fix_command or AQA_FIX_COMMAND. Edit only the campaign branch aqa/<campaign-id>, then let the loop retest failed shards and update that one draft PR.

Stop when scripted shards are green or when any safety cap trips:
- max_iterations 3 (stop_reason max_iterations)
- max_wall_time 1h (stop_reason max_wall_time)
- max_pr_updates 5 (stop_reason max_pr_updates)

--backend selects local, vm, or cloud. --workers is the concurrency cap. --max-spend is required for cloud and is the currency ceiling (sample 10 USD).

Never run gh pr merge. Never merge into the default branch. If a cap trips, leave the pull request open and report the remaining finding ids.
```
