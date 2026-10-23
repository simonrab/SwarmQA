# SwarmQA

SwarmQA tests iOS and macOS apps. It is a command-line tool called `aqa`.

SwarmQA opens your app, uses it like a person, and looks for problems. It records each problem with screenshots, video and the steps to make it happen again. It writes one report for each run.

SwarmQA finds problems. It does not fix them. A coding agent (Claude Code or Codex) can read the report, fix the code and ask SwarmQA to check the fix. SwarmQA never changes your code and never opens a pull request.

## Words in this document

| Word | Meaning |
| --- | --- |
| Campaign | One run of SwarmQA against one app. It makes one report. |
| Intent | A file that tells SwarmQA what to test. It has a goal, or exact steps, or both. |
| Shard | One unit of work in a campaign, usually one intent. |
| Worker | One process that runs one shard on one device. |
| Device | An iOS Simulator, or the Mac itself. |
| Driver | The part that controls the app: it taps, types and reads the screen. |
| Finding | One problem that SwarmQA found. |
| Crawl | A shard with no goal: SwarmQA tries every control it can reach. |

## What SwarmQA can do

- **Follow a goal.** For example, "Open Settings and turn on reminders". SwarmQA finds the way through the app.
- **Run exact steps.** It taps, types and checks in the order you write.
- **Crawl the app.** It tries every button, field and link it can reach.
- **Check every screen.** It finds crashes, hangs, taps that do nothing, error messages, cut-off text, overlapping views, small tap targets, missing labels and low contrast.
- **Judge screens with a model (optional).** A model can flag screens that look wrong or confusing. These findings are marked "advisory".
- **Compare screenshots** with saved PNG baselines.
- **Test each pull request** on GitHub, and post the result.
- **Run faster on repeat.** It saves the path to each goal and replays it next time.
- **Use several devices at once.** It gives each worker its own simulator.

## Requirements

- A Mac with Xcode 26 or later, for real apps. (The tests run on Linux too, with a fake driver.)
- Python 3.11 or later. `uv` installs it for you.
- An API key for Anthropic or OpenAI, only if you use a model.

## Install

1. Install the `aqa` command with [uv](https://docs.astral.sh/uv/):

   ```bash
   uv tool install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'
   ```

   - Keep the quotes.
   - The words in brackets are extras. Use `anthropic` or `openai` for a model. Use `mcp` for coding agents.
   - To add an extra later, run the command again with `--reinstall`.
   - `pipx install` with the same text also works.

2. Check the Mac:

   ```bash
   aqa doctor
   ```

   `aqa doctor` checks Xcode, simulators, permissions, tools, API keys and your config. It tells you how to fix each problem. See [docs/doctor.md](docs/doctor.md).

3. To use the runner driver (recommended, see below), build the runner once. You need a clone of this repository for this step:

   ```bash
   git clone https://github.com/simonrab/swarmqa
   swarmqa/agents/swarm-runner/build.sh
   ```

   The build goes to `~/.aqa/swarm-runner/`. The installed `aqa` command finds it there.

## Try it in one command

```bash
aqa try MyApp.app            # an app you already built
aqa try --scheme MyApp       # or, in your Xcode project folder: build, then test
```

`aqa try` needs no config and no intents. It picks a simulator, crawls the app for about 5 minutes and opens the report in your browser. It is free: it uses no model unless you add `--model`. Add `--goal "Open Settings"` to also follow a goal. Run `aqa try --save` to keep the settings as a config file. See [docs/try.md](docs/try.md).

## Quick start

1. Go to your app's folder and make a config file:

   ```bash
   aqa init
   ```

   This writes `aqa.config.toml`, `templates/issue.md`, and the empty folders `intents/` and `reports/`.

2. Edit `aqa.config.toml`. Set the app and the driver:

   ```toml
   [app]
   path = "/path/to/MyApp.app"   # for iOS: the Simulator build of the app
   platform = "ios"              # or "macos"
   source_dir = "."              # your app's source code (optional, see below)

   [driver]
   kind = "runner"

   [explorer]
   engine = "agent"
   ```

3. Write an intent. Make the file `intents/settings.md`:

   ```markdown
   # Open settings

   Open "Settings" and turn on "Due date reminders".
   ```

   Put the names of buttons and labels in quotes. SwarmQA looks for them on the screen.

4. Run the campaign:

   ```bash
   aqa run
   ```

   SwarmQA prints the report folder, for example `reports/20261009T194738Z-f0cb1e/`.

5. Read the report:

   ```bash
   aqa report
   ```

`aqa run` exits with 0 when the run is clean. It exits with 1 when a scripted step fails. It exits with 2 for a usage or config error. `fail_on` in `[campaign]` changes this.

## Choose a driver

Set `kind` in `[driver]`.

| `kind` | How it works | Use it for |
| --- | --- | --- |
| `runner` | An XCUITest runner inside the simulator or on the Mac. Fast and reliable. | iOS and macOS. Recommended. |
| `legacy` | `simctl` and `idb` for iOS. AppleScript for macOS. | Older setups. iOS needs `idb` (`brew install facebook/fb/idb`). |
| `fake` | A fake app in memory. | Tests and dry runs. |
| `auto` (default) | `legacy` on a Mac, `fake` on other systems. | |

For macOS, give Accessibility permission to the app that runs `aqa` (System Settings > Privacy & Security). Video also needs Screen Recording permission. The first runner session on macOS asks you to approve automation mode. See [docs/driver.md](docs/driver.md).

For iOS, check the simulator names with `xcrun simctl list devices available`. Xcode 26 has "iPhone 17", not "iPhone 16".

## Write intents

An intent is a markdown file (`.md`) or a recorded flow (`.json`). Put intents in `intents/`.

**A goal.** SwarmQA finds the way.

```markdown
# Add a task

Tap "Add Task", type a title, and tap "Save".
```

**Exact steps.** Add a `## Steps` section. SwarmQA does each step in order.

```markdown
# Save a task

## Steps

- click button "Add Task"
- type "Buy milk" into "Title"
- click button "Save"
- assert "Buy milk" exists
```

Other step forms: `key cmd+return`, `menu File > New`, `wait for "Done" 5s`, `scroll -3`, `screenshot "name"`, `relaunch`.

To record a flow by hand, run `aqa record --app /path/to/MyApp.app --out intents/flow.json --interactive`. See [docs/intents.md](docs/intents.md). `examples/intents/smoke.md` is an example.

## Use a model

Without a model, SwarmQA uses free rules: it taps controls whose names match the goal. A model makes goal-following and screen judging much better.

```toml
[llm]
enabled = true
provider = "anthropic"   # or "openai"
max_cost = 5.0           # stop model calls after this amount
```

```bash
export ANTHROPIC_API_KEY=...
```

The model is off by default, so SwarmQA spends no money until you turn it on. See [docs/llm.md](docs/llm.md).

## Read the report

Each campaign writes one folder, `reports/<campaign-id>/`. Open `report.html` in a browser, or run `aqa report --html`. The page has no external links, so you can zip the folder and share it.

| File | Contents |
| --- | --- |
| `report.html` | Start here. Every finding with screenshots, video clip, steps and the replay command; the screens covered. |
| `summary.md` | The same, as text. |
| `findings.json` | Every finding, with its evidence. |
| `findings/<id>.md` | One finding: steps, screenshots, video clip. |
| `findings/<id>.replay.json` | The steps that make the finding happen again. |
| `media/` | Screenshots and video. |
| `screen_graph.json` | Every screen SwarmQA saw and how they connect. |

Useful commands:

| Command | What it does |
| --- | --- |
| `aqa report` | Prints the latest `summary.md`. |
| `aqa status` | Shows the progress of a running campaign. |
| `aqa replay reports/<id>/findings/<finding>.replay.json` | Runs a finding again. Exits with 1 if the problem is still there. |
| `aqa verify <finding-id>` | Rebuilds the app and replays a finding on 2 devices. Exits with 0 when it is fixed. |
| `aqa file-issues` | Shows the GitHub or Linear issues it would file. Add `--yes` to file them. |

### How SwarmQA keeps findings trustworthy

- **It checks findings again.** Before it writes the report, SwarmQA replays each crash, hang, dead tap and failed step on a fresh launch of the app. (Layout findings come from the screen itself, so they are not replayed.) The report shows "Confirmed 1/1", "Flaky 1/2" or "Not reproduced 0/1".
- **A finding that does not happen again does not fail the run.** It becomes advisory, and the report lists it under "To check".
- **It groups repeats.** The same problem in several places is one finding, for example `Small tap target: button "Share" (+3 more)`.
- **Model-only findings are marked "advisory".** They never fail a run or a GitHub check unless you ask for it.

Change these rules in the `[findings]` table. See [docs/trust.md](docs/trust.md).

If you set `app.source_dir`, each finding lists the source files that are likely involved. SwarmQA also remembers findings across runs in `<source_dir>/.aqa/state/`.

## Use several devices

By default a campaign runs 2 workers on this Mac. To give each worker its own simulator, turn on the swarm:

```toml
[campaign]
workers = 4

[swarm]
enabled = true
golden = "iPhone 17"   # SwarmQA makes copies of this simulator; it must be shut down
crawl = 2              # optional: also crawl the app with 2 devices (needs engine = "agent")
```

- SwarmQA copies the simulator once. It keeps the copies running for the next campaign.
- The number of devices is limited by the Mac's memory and cores. An 8 GB Mac gets 1 simulator.
- With `crawl`, the devices share the work. Two devices never try the same control.

See [docs/orchestrator.md](docs/orchestrator.md).

## Run faster on repeat

**Replay cache.** When SwarmQA reaches a goal, it saves the path in `<source_dir>/.aqa/flows/`. The next run follows the saved path first. It calls no model for those steps. It still checks every screen. If a step no longer works, SwarmQA finds a new way for that step only and saves the new path. You need `app.source_dir` or `flows.cache_dir` for this. To turn it off, set `cache = false` in `[flows]`.

**Intents from a code change.** A model reads your change and writes intents for the flows that are most at risk:

```bash
aqa flows propose --base main     # writes intents to intents/from-diff/
aqa run --from-diff main          # writes them and runs them
```

`aqa flows list` shows the saved paths. See [docs/flows.md](docs/flows.md).

## Test pull requests on GitHub

```bash
aqa watch            # test new pull requests and merges as they appear
aqa watch --pr 12    # test one pull request now
```

For each commit, SwarmQA makes a clean copy of the repository, builds the app, runs one campaign per platform and prints the report. Your own working folder is not touched.

- To post results to GitHub, set `report = "check"`, `"comment"` or `"both"` in `[github]`. By default nothing is posted.
- To also test the flows that the change puts at risk, set `from_diff = true` in `[flows]`.
- To run on every push, use the GitHub Action in [integrations/github-action](integrations/github-action/README.md) on a self-hosted Mac runner.

To build one commit yourself, run `aqa build --sha <commit>`. See [docs/github.md](docs/github.md) and [docs/build.md](docs/build.md).

## Use with a coding agent

Claude Code and Codex control SwarmQA through its MCP server, `aqa mcp`. You need the `mcp` extra.

The agent does these steps:

1. It starts a campaign.
2. It reads the findings.
3. It fixes the code on a new branch.
4. It asks SwarmQA to check the fix (`verify_fix`).
5. It opens a pull request when the check passes.

| Agent | Setup |
| --- | --- |
| Claude Code | [integrations/claude-code](integrations/claude-code/README.md): a `/qa` skill and an `.mcp.json` entry |
| Codex CLI | [integrations/codex](integrations/codex/README.md): a `~/.codex/config.toml` entry and an `AGENTS.md` section |

See [docs/agents.md](docs/agents.md).

## Compare screenshots

Turn on `[visual]` and save baselines. SwarmQA compares new screenshots with them pixel by pixel.

```bash
aqa baseline update --from-dir reports/<campaign-id>/media --baseline-dir baselines
```

See [docs/visual.md](docs/visual.md).

## All commands

| Command | Purpose |
| --- | --- |
| `aqa try` | Test an app with no setup and open the report. |
| `aqa init` | Make `aqa.config.toml` and the folders. |
| `aqa doctor` | Check the Mac and the config. |
| `aqa run` | Run a campaign. |
| `aqa status` | Show campaign progress. |
| `aqa report` | Print the latest summary (`--html` opens the page). |
| `aqa record` | Record a flow. |
| `aqa replay` | Run one finding again. |
| `aqa verify` | Rebuild and check that a finding is fixed. |
| `aqa build` | Build one commit in a clean copy. |
| `aqa watch` | Test GitHub pull requests and merges. |
| `aqa flows` | Write intents from a change; list saved paths. |
| `aqa file-issues` | File findings in GitHub or Linear. |
| `aqa baseline update` | Save screenshots as baselines. |
| `aqa mcp` | Start the MCP server for coding agents. |

Each command has `--help`.

## Status

SwarmQA is in active development. The plan and its status are in [docs/build-plan.md](docs/build-plan.md).

- Tested on a real Mac: the runner driver on iOS and macOS, the device pool, the replay cache and the builder.
- Benchmark: on the test app in `fixtures/PlantedBugs`, SwarmQA finds 7 of 8 planted bugs without a model, with 1 false finding.
- Not yet tested with live Anthropic or OpenAI calls.
- Not yet built: remote Mac hosts (`--remote`).

## Work on SwarmQA

```bash
git clone https://github.com/simonrab/swarmqa
cd swarmqa
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev,mcp]"
.venv/bin/pytest
```

The tests use the fake driver. They do not need a Mac, Xcode or `idb`.

Do not clone into `~/Documents` or `~/Desktop` when those sync to iCloud. macOS marks the files there as hidden, Python skips a hidden `.pth` file, and `aqa` then fails with "No module named swarmqa". `aqa doctor` reports this problem.

## More detail

| Topic | Document |
| --- | --- |
| `aqa try` | [docs/try.md](docs/try.md) |
| The report | [docs/report.md](docs/report.md) |
| Confirmation and grouping | [docs/trust.md](docs/trust.md) |
| All config fields | [docs/config.md](docs/config.md) |
| Intents | [docs/intents.md](docs/intents.md) |
| Drivers | [docs/driver.md](docs/driver.md) |
| Agent explorer | [docs/explorer-v2.md](docs/explorer-v2.md) |
| Checks | [docs/checks.md](docs/checks.md) |
| Models | [docs/llm.md](docs/llm.md) |
| Campaigns, workers and the swarm | [docs/orchestrator.md](docs/orchestrator.md) |
| Devices | [docs/devices.md](docs/devices.md) |
| Replay cache and intents from a change | [docs/flows.md](docs/flows.md) |
| Findings and evidence | [docs/evidence.md](docs/evidence.md) |
| Visual baselines | [docs/visual.md](docs/visual.md) |
| GitHub | [docs/github.md](docs/github.md) |
| Builder | [docs/build.md](docs/build.md) |
| Fix checks | [docs/verify.md](docs/verify.md) |
| Coding agents and MCP | [docs/agents.md](docs/agents.md) |
| `aqa doctor` | [docs/doctor.md](docs/doctor.md) |
