# SwarmQA

SwarmQA is a command-line tool. It runs a QA campaign against a Mac app or an iOS Simulator app and writes one report.

A campaign can replay written steps. It can hunt toward a goal by clicking labeled controls. It can run a test command you already have. It can compare new screenshots with saved PNG baselines.

## iOS Simulator

Set `app.platform` to `ios` and `app.simulator` to a device name. On a Mac with Xcode 26, this machine has iPhone 17, not iPhone 16. Check the names on your Mac with `xcrun simctl list devices available`.

```toml
[app]
platform = "ios"
simulator = "iPhone 17"
```

The driver uses `xcrun simctl` to boot, install, launch, and screenshot. Taps and the accessibility tree use the `idb` command. `idb` is a separate install:

```bash
brew tap facebook/fb
brew install facebook/fb/idb
```

Without `idb`, launch and screenshots can run, and the first tap stops.

## Several workers

The default is 2 workers on this machine. Mac workers on one desktop share one screen. iOS workers each need their own Simulator name in `app.simulators`.

```toml
[app]
platform = "ios"
simulators = ["iPhone 17", "iPhone 17 Pro"]

[campaign]
workers = 2
```

## Install

Install the `aqa` command with [uv](https://docs.astral.sh/uv/). uv fetches a suitable Python (3.11 or newer) and keeps SwarmQA in its own environment, so the macOS system Python is left alone. List the extras you want in the brackets: `anthropic` or `openai` for the model provider, `mcp` for coding agents.

```bash
uv tool install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'
```

Keep the quotes: the name, the extras, `@` and the URL are one requirement. `uv tool install git+https://github.com/simonrab/swarmqa` installs the core without extras. To add extras later, run the full command again with `--reinstall`. `pipx install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'` works the same way.

Then check the machine:

```bash
aqa doctor
```

`aqa doctor` checks Python, Xcode, simulator runtimes and devices, `idb`, Accessibility permission, `ffmpeg`, Tart, model API keys, the optional extras, and `aqa.config.toml`. `--github` adds `gh` login and token scopes; `--json` prints machine-readable output. It exits 0 when nothing fails. See [docs/doctor.md](docs/doctor.md).

To work on SwarmQA itself:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Quick start

```bash
aqa init
```

`aqa init` writes `aqa.config.toml`, `templates/issue.md`, and empty `intents/` and `reports/` directories. Pass `--dir` to choose a different directory. Turn on the agent explorer and a model:

```toml
[explorer]
engine = "agent"

[llm]
enabled = true
provider = "anthropic"   # reads ANTHROPIC_API_KEY
```

```bash
export ANTHROPIC_API_KEY=...
aqa run --app /path/to/MyApp.app --intent intents/smoke.md
```

That command reads `aqa.config.toml`. The defaults are a local backend, 2 workers, and video on. Without `[llm]` the agent explorer still runs with its free heuristic. It prints the report directory, `reports/<campaign-id>/`.

```bash
aqa report
aqa status
aqa record --app /path/to/MyApp.app --out intents/flow.json --interactive
aqa replay reports/<campaign-id>/findings/<id>.replay.json
```

`aqa report` prints the latest `summary.md`. `aqa status` prints campaign progress. `aqa record --interactive` saves a flow you click through. `aqa replay` reruns one finding and exits 1 if it still reproduces. `examples/intents/smoke.md` is a short scripted intent you can copy.

A live Mac session needs Accessibility permission for the process that starts the app. Video also needs Screen Recording permission. Details are in [docs/driver.md](docs/driver.md).

To copy screenshots into the baseline directory:

```bash
aqa baseline update --from-dir reports/<campaign-id>/media --baseline-dir baselines
```

## Reports and issues

Each campaign writes one report under `reports/<campaign-id>/`. Start with `summary.md`. `findings.json` lists every finding with its evidence. Screenshots and video, when recorded, are under `media/`. A finding can include a replay JSON file.

Campaigns do not file issues on their own. To file a campaign's findings into GitHub or Linear (as configured in `[issues]`):

```bash
aqa file-issues                        # dry run: print what would be filed
aqa file-issues --tracker github --yes # file into GitHub
```

Screenshot checks compare pixels with saved baselines. See [docs/visual.md](docs/visual.md).

## GitHub

`aqa watch` tests new and updated PRs and merges into the default branch: it builds each commit in a clean checkout, runs one campaign per platform, and prints the report. `aqa watch --pr 12` tests one PR now. On a self-hosted Mac runner, the GitHub Action in [integrations/github-action](integrations/github-action/README.md) runs `aqa run --github-sha` on every push. With `[github] report = "check"`, `"comment"` or `"both"`, results are posted as a check and one PR comment, edited in place; nothing is posted by default. See [docs/github.md](docs/github.md).

## Coding agents (MCP)

Claude Code and Codex drive SwarmQA through its MCP server, `aqa mcp` (needs the `mcp` extra). The agent starts a campaign, reads the findings, fixes the code, calls `verify_fix` to rebuild and replay the finding on two devices, and opens the pull request itself once the fix passes. SwarmQA never edits code or opens PRs.

| Agent | Setup |
| --- | --- |
| Claude Code | [integrations/claude-code](integrations/claude-code/README.md): a `/qa` skill and an `.mcp.json` entry |
| Codex CLI | [integrations/codex](integrations/codex/README.md): a `~/.codex/config.toml` entry and an `AGENTS.md` section |

The loop and the tool reference are in [docs/agents.md](docs/agents.md). The old `--pr-mode` flag and `[pr]` config are gone; an old config with `[pr]` still loads and prints a deprecation warning.

## Tests

```bash
python -m pytest
```

Tests use the fake driver. They do not need a Mac, Xcode, or `idb`.

## More detail

| Topic | Doc |
| --- | --- |
| Config fields | [docs/config.md](docs/config.md) |
| macOS and iOS drivers | [docs/driver.md](docs/driver.md) |
| Orchestrator | [docs/orchestrator.md](docs/orchestrator.md) |
| Visual baselines | [docs/visual.md](docs/visual.md) |
| Coding agents and MCP | [docs/agents.md](docs/agents.md) |
| GitHub trigger | [docs/github.md](docs/github.md) |
| Per-SHA builder | [docs/build.md](docs/build.md) |
| `aqa doctor` | [docs/doctor.md](docs/doctor.md) |
