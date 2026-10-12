# aqa try

`aqa try` tests an app with no setup. You do not write a config file, intents, or choose a driver. After about 5 minutes of exploring (plus start-up time) you get a report of the problems it found.

## What you need

- A Mac with Xcode. Open Xcode once so that it installs its tools.
- For iOS apps: one iPhone Simulator. Xcode installs one by default.
- The XCUITest runner build. See [Build the runner](#build-the-runner).

## Quick start

1. Go to your project folder: `cd MyProject`.
2. Run `aqa try`.
3. Wait. SwarmQA builds the app, opens it, and visits its screens.
4. Read the report. It opens in your browser at the end.

You can also give a target:

```sh
aqa try path/to/MyApp.app            # test a built app
aqa try path/to/MyApp.xcodeproj      # build the project, then test it
aqa try --scheme MyApp               # in a project folder with many schemes
```

The example app in this repo works too: `aqa try fixtures/PlantedBugs`.

## What it does

1. It finds the app.
   - A `.app`: it reads `Info.plist` to get the bundle id and the platform (iOS Simulator or macOS).
   - A project or a folder: it builds the last commit (HEAD) with the per-SHA builder (see [build.md](build.md)). It finds the scheme with `xcodebuild -list`. It picks iOS when the scheme supports iOS, else macOS.
2. It picks a device. For iOS it uses `--simulator`, else "iPhone 17", else any available iPhone. For macOS it uses this Mac.
3. It prints what it will do: the app, the device, the time budget, and whether it uses a model.
4. It runs your goals (`--goal`) first. Then it crawls the app: it taps every control it finds, screen by screen, and runs the checks on each screen.
5. It writes the report under `reports/<campaign id>/`, prints the number of findings by severity, and opens the HTML report.

The run exits with 0 when the test ran, with or without findings. It exits with 2 when it could not run: bad input, a failed build, no simulator, or no runner.

## Options

| Option | What it does |
| --- | --- |
| `TARGET` | A `.app`, an `.xcodeproj`, an `.xcworkspace`, or a folder. Default: the current folder. |
| `--scheme NAME` | The scheme to build. Default: found with `xcodebuild -list`. |
| `--platform ios\|macos` | The platform to build when the scheme supports both. |
| `--simulator NAME_OR_UDID` | The iOS Simulator to use. |
| `--minutes N` | The time budget. Default: 5. |
| `--goal TEXT` | A task to try before the crawl, for example `--goal "Add a plant"`. You can give it more than once. |
| `--model` | Use a model. It needs `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. |
| `--config PATH` | Start from your config file instead of the defaults. |
| `--report-root DIR` | Where to write reports. Default: `reports`. |
| `--no-open` | Do not open the HTML report. |
| `--save` | Write a starter `aqa.config.toml` and `intents/example-goal.md`. |

## Time budget

`--minutes` sets the time to explore. The goals and the crawl share it equally. For example, `--minutes 6` with two goals gives each goal and the crawl 2 minutes. Start-up time comes on top: SwarmQA must install the app and start the runner for each goal and for the crawl. On a slow Mac this takes 1 to 3 minutes each time. The first run on a new simulator takes longer.

## Models

Without `--model`, the run is free. The crawl uses built-in rules and makes no model calls.

With `--model`, SwarmQA uses Anthropic when `ANTHROPIC_API_KEY` is set, else OpenAI when `OPENAI_API_KEY` is set. Model calls cost money. The provider package must be installed, for example `uv tool install 'swarmqa[anthropic]'`. If the key or the package is missing, the run stays free and says why.

## Projects and uncommitted changes

`aqa try` builds the last commit in a clean checkout under `~/.aqa`. Your uncommitted changes are not in that build. SwarmQA prints a warning when the working tree has changes. Commit them to test them.

The folder must be in a git repository with at least one commit. If it is not, build the app in Xcode and pass the `.app`.

When SwarmQA builds the project, it sets `app.source_dir` to the repository. Then the report can name the source files that likely cause a finding, and later runs can reuse saved goal paths.

## Build the runner

SwarmQA drives the app with an XCUITest runner. You build the runner once.

1. Clone the repo: `git clone https://github.com/simonrab/swarmqa`.
2. Go to it: `cd swarmqa`.
3. Build the runner for your platform: `agents/swarm-runner/build.sh ios` (or `macos`).
4. Run `aqa try` again.

When you run SwarmQA from a source checkout, it builds the runner by itself the first time. The build is stored under `~/.aqa/swarm-runner/DerivedData`. Set `SWARM_RUNNER_DERIVED_DATA` to use another folder.

## Next steps

1. Run `aqa try --save`. It writes `aqa.config.toml` with your app, platform, the runner driver, and the agent engine. It also writes an example goal in `intents/`. It does not replace files that exist.
2. Edit `intents/example-goal.md`, or add more intents.
3. Run `aqa run` to test with your config.

## Fix common problems

| Message | Fix |
| --- | --- |
| no app or Xcode project here | Pass a `.app` or a project, or `cd` to the project folder. |
| several schemes | Pass `--scheme NAME`. |
| is not in a git repository | Commit the project, or pass a built `.app`. |
| the build failed | Open the log path in the message. Fix the build in Xcode, commit, and run again. |
| No iPhone Simulator is installed | In Xcode, open Settings > Components and add an iOS Simulator runtime. |
| built for iPhone devices | Build for an iOS Simulator destination. |
| the XCUITest runner is not built | Follow [Build the runner](#build-the-runner). |
