# Per-SHA builder

`swarmqa.build` checks out one commit of an app repo and builds its iOS
Simulator and macOS apps, without touching anyone's working copy. The
GitHub watcher calls it once per PR SHA; `aqa verify` can use it to rebuild
a fix; `aqa build` runs it by hand.

```python
from swarmqa.build import BuildSettings, ShaBuilder, build_failure_finding

builder = ShaBuilder(BuildSettings(repo="git@github.com:acme/App.git"))
report = builder.build_many(sha, ("ios", "macos"))   # never raises
for artifact in report.artifacts:                     # swarmqa.models.BuildArtifact
    print(artifact.platform, artifact.app_path, artifact.bundle_id)
findings = report.findings()                          # one critical Finding per failed platform

artifact = builder.build(sha, "ios")                  # or raise BuildFailed (.failure)
```

## What happens

1. **Mirror.** `~/.aqa/repos/<repo>.git` is a `git clone --mirror` of the
   source (a URL, or a local path cloned with `--no-hardlinks`). It is
   fetched only when the SHA is missing; an unadvertised SHA (a PR head) is
   then fetched by id. Git never prompts (`GIT_TERMINAL_PROMPT=0`).
2. **Checkout.** `git worktree add --detach` into
   `~/.aqa/checkouts/<repo>/<full sha>`, reused when it already exists at
   that SHA. Submodules are initialised when `.gitmodules` exists.
3. **Build**, in one of two modes per platform:
   - **command**: `build.<platform>.command` (or `build.command`) runs
     through `/bin/sh -c` in the checkout root. It must print the `.app`
     path (a bare path or `ios=/path/App.app` both work) or leave a `.app`
     in the checkout or `$AQA_DERIVED_DATA`. It gets `AQA_PLATFORM`,
     `AQA_SHA`, `AQA_CHECKOUT`, `AQA_DERIVED_DATA`, `AQA_CONFIGURATION` and
     `AQA_OUTPUT_DIR`.
   - **auto-detect**: the project is `build.project` (a directory, or a
     `.xcodeproj`/`.xcworkspace`, relative to the checkout) or the only one
     at the checkout root, a workspace winning over a project. The scheme is
     `build.scheme`, else it comes from `xcodebuild -list -json`: the only
     scheme, the one named after the project, or the only one that is not a
     test or `Pods-` scheme. Anything ambiguous is a `detect` failure that
     names the setting to add. xcodebuild runs with
     `-derivedDataPath ~/.aqa/DerivedData/<repo>`, destination
     `generic/platform=iOS Simulator` or `platform=macOS`, `ARCHS=<host
     arch>` for the simulator, `COMPILER_INDEX_STORE_ENABLE=NO`, and
     `CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM=`
     (`build.signing = "project"` keeps the project's signing).
4. **Store.** The `.app` is copied with `ditto --noextattr --noqtn` to
   `~/.aqa/builds/<repo>/<sha>/<platform>/app/`, next to `build.log` and
   `artifact.json`. `codesign --verify --strict` must pass. The bundle id is
   `CFBundleIdentifier` from `Info.plist` (`Contents/Info.plist` on macOS).
   The next build of the same SHA and platform returns the stored artifact
   at once; `force=True` / `--force` rebuilds.

Everything lives under `~/.aqa` (or `$AQA_HOME`, or `build.root`), outside
iCloud-synced folders on purpose: codesign rejects bundles written under a
synced `~/Documents` or `~/Desktop` ("resource fork, Finder information, or
similar detritus not allowed").

## Concurrency

Host-wide `flock` leases from `swarmqa.devices.locks`:

| Lock | Held for |
|---|---|
| `build-sha-<repo>-<sha>` | the whole build of that SHA, so two builds of one SHA run one after the other and the second reuses the first's artifact |
| `build-repo-<repo>` | clone, fetch, `worktree add/remove/prune` only |
| `build-dd-<repo>` | xcodebuild (or the build command) and the copy out of DerivedData |

Builds of different SHAs of one repo check out in parallel but compile one at
a time, because they share DerivedData (which is what makes the second SHA
fast). Different repos build in parallel.

## Failures

A failure is a `BuildFailure(repo, sha, platform, stage, message, log_path,
log_tail)`, where `stage` is `checkout`, `detect`, `build`, `product` or
`codesign`, and `log_tail` holds the log's `error:` lines followed by its
last 30 lines. `build_failure_finding(failure)` turns it into a critical
`Finding` with `kind="launch"` (category `crash`, as for an app that cannot
launch; it also lets `aqa verify` check the fix by rebuilding and
launching, without a replay flow). Its id is `build-<platform>-<sha12>`;
its fingerprint uses the platform, stage and first error line, so the same
break on the next commit deduplicates.

## Cleanup

`builder.prune(keep=N, max_age_s=S)` or `aqa build --prune [--keep N]
[--max-age-days D]` removes all but the N most recently used checkouts of
a repo (default `build.keep = 20`), and any unused for longer than S, plus
their stored builds. Checkouts whose SHA lock is held are skipped.

## verify_fix

`swarmqa.build.verify_builder(...)` returns a
`swarmqa.verify.build.Builder`, so `core.verify(..., builder=...)` builds the
committed `HEAD` of the app's repo in a clean checkout. The repo is
`build.repo`, else the git top level of `app.source_dir` (whose
subdirectory then becomes `build.project`). On success it sets
`config.app.path` and `config.app.bundle_id` to the new build. A local repo
with uncommitted changes is an error, because the clean checkout would not
contain the fix. With no repo configured it falls back to `build_app`
(`app.build_command` in place).

## Config: `[build]`

```toml
[build]
repo = "git@github.com:acme/App.git"  # URL or local path; default app.source_dir
project = "apps/App"                   # dir, .xcodeproj or .xcworkspace in the checkout
scheme = "App"                         # default: auto-detect
configuration = "Debug"
# command = "./scripts/build.sh"       # command mode for both platforms
# signing = "adhoc"                    # or "project"
# xcodebuild_args = ["-skipMacroValidation"]
# timeout_s = 1800
# keep = 20
# root = "~/.aqa"                      # checkout_root, repo_cache_root, derived_data_root, artifact_root override parts

[build.ios]
# command, scheme, project, destination, configuration, archs, xcodebuild_args

[build.macos]
scheme = "App macOS"
```

Unknown keys are errors.

## CLI

```sh
aqa build --sha <sha|ref> [--repo URL|PATH] [--platform ios|macos|all]
          [--project PATH] [--scheme NAME] [--configuration NAME] [--force] [--json]
aqa build --prune [--keep N] [--max-age-days D] [--repo URL|PATH]
```

Also runnable as `python -m swarmqa.build`. Exit 0 when every platform
built, 1 when any failed (with `--json`, the findings are in the output), 2
for usage or config errors.

## Testing

Every git, xcodebuild, ditto and codesign call goes through `runner(args,
**kwargs)` (`swarmqa.build.runner.build_runner` by default, normalised by
`swarmqa.devices.commands.run`), so `tests/test_build.py` runs on Linux
with a fake that plays back a recorded `xcodebuild -list -json`.
