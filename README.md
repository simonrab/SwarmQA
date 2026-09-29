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

## Install and run

Install the `aqa` command with [uv](https://docs.astral.sh/uv/). uv fetches a suitable Python (3.11 or newer) and keeps SwarmQA in its own environment, so the macOS system Python is left alone.

```bash
uv tool install git+https://github.com/simonrab/swarmqa
aqa init
```

`pipx install git+https://github.com/simonrab/swarmqa` works the same way. To work on SwarmQA itself:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

`aqa init` writes `aqa.config.toml`, `templates/issue.md`, and empty `intents/` and `reports/` directories. Pass `--dir` to choose a different directory.

```bash
aqa run --app /path/to/MyApp.app --intent intents/smoke.md
```

That command reads `aqa.config.toml`. The defaults are a local backend, 2 workers, video on, and PR mode off (report only, no branch or PR). It prints the report directory, `reports/<campaign-id>/`.

```bash
aqa report
aqa status
aqa record --app /path/to/MyApp.app --out intents/flow.json --interactive
```

`aqa report` prints the latest `summary.md`. `aqa status` prints campaign progress. `aqa record --interactive` saves a flow you click through. `examples/intents/smoke.md` is a short scripted intent you can copy.

A live Mac session needs Accessibility permission for the process that starts the app. Video also needs Screen Recording permission. Details are in [docs/driver.md](docs/driver.md).

To copy screenshots into the baseline directory:

```bash
aqa baseline update --from-dir reports/<campaign-id>/media --baseline-dir baselines
```

## Reports and the PR loop

Each campaign writes one report under `reports/<campaign-id>/`. Start with `summary.md`. Screenshots and video, when recorded, are under `media/`. A finding can include a replay JSON file.

When the campaign has results, human mode writes one draft pull request and stops. It does not change the app under test. If the `gh` command is available, that draft is opened with `gh pr create --draft`. The draft file is still written when `gh` is missing.

Screenshot checks compare pixels with saved baselines. See [docs/visual.md](docs/visual.md).

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
| PR loop | [docs/agents.md](docs/agents.md) |
