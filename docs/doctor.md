# aqa doctor

`aqa doctor` checks that this machine can run SwarmQA campaigns and prints a fix for anything that is not ready.

```bash
aqa doctor                   # reads aqa.config.toml in the current directory
aqa doctor --config path/to/aqa.config.toml
aqa doctor --github          # also check gh login and token scopes
aqa doctor --json            # machine-readable output
```

Each check reports `ok`, `warn`, `fail` or `skip`, a one-line message, and a `fix:` hint when it is not `ok`. The command exits 0 when no check fails and 1 otherwise, so `warn` never breaks a script. Off macOS, the Mac-only checks report `skipped (not macOS)`.

| Check | What it runs | Status |
| --- | --- | --- |
| `python` | the running interpreter | `fail` below 3.11 |
| `xcode` | `xcode-select -p`, `xcodebuild -version` | `fail` when no developer directory is selected or only the Command Line Tools are present. Mac only |
| `simulators` | `xcrun simctl list runtimes -j`, `xcrun simctl list devices available -j` | `fail` when `app.platform = "ios"` and there is no iOS runtime, no available device, or a configured `app.simulator`/`app.simulators` entry is missing; `warn` for the same on macOS-only projects. Mac only |
| `idb` | looks for `idb` on `PATH` | `warn` when missing (the legacy iOS driver needs it for taps). Mac only |
| `accessibility` | `osascript -l JavaScript` calling `AXIsProcessTrusted()` | `warn` when the terminal lacks Accessibility permission or the answer is unknown. Best effort. Mac only |
| `ffmpeg` | looks for `ffmpeg` on `PATH` | `warn` when missing (video clips and frames) |
| `tart` | Apple Silicon check, `vm.tart_bin` on `PATH`, `tart --version` | `fail` when `backend = "vm"` and Tart cannot run; otherwise `warn`. Mac only |
| `llm keys` | the `[llm]` provider's key variable (`api_key_env`, default `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) | `skip` unless `llm.enabled`; `fail` when the variable is empty. Only the variable name is printed, never its value |
| `extra: anthropic`, `extra: openai`, `extra: mcp` | whether each optional package imports | `fail` when the enabled `[llm]` provider's extra is missing; otherwise `warn` |
| `config` | `load_config` on the config file | `fail` with the field errors when invalid; `warn` when the file is missing or still has a deprecated `[pr]` table |
| `github` (with `--github`) | `gh auth status` | `fail` when `gh` is missing, not logged in, or the listed scopes lack `checks:write` and `pull-requests:write`; `warn` when the scopes are not shown (fine-grained or app tokens) or only the classic `repo` scope is listed |

The Accessibility check asks macOS whether the process that runs `aqa` is trusted. The grant belongs to the terminal app (Terminal, iTerm, or the agent host that starts `aqa`), so grant it in System Settings > Privacy & Security > Accessibility and restart that app.

Classic `gh` OAuth tokens show scopes such as `repo`, which covers pull requests. Creating check runs usually needs a GitHub App or a fine-grained token with Checks write access, so a `repo`-only token is a `warn`, not a `fail`.

## JSON

```json
{
  "ok": true,
  "checks": [
    {"name": "python", "status": "ok", "message": "Python 3.12.4", "fix": ""}
  ]
}
```

`ok` is false when any check has status `fail`.

## For tests

`swarmqa.doctor.run_doctor(config_path, github=False, env=DoctorEnv(...))` runs the checks. `DoctorEnv` holds every host dependency: `runner` (the `swarmqa.devices.commands` runner seam), `which`, `platform`, `machine`, `environ`, `find_spec` and `python_version`. Tests pass fakes for all of them, so the Mac paths run on Linux.
