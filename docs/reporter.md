# Scripted explorer and reporter

Scripted shards run through `run_scripted`. Findings land in the campaign
directory as markdown plus a version-1 replay flow. `create_issues` always
writes a local ticket and, when configured, files the same evidence to GitHub
or Linear.

## Running a scripted shard

```python
run_scripted(shard, driver, config, *, worker_id, work_dir) -> WorkerResult
```

`work_dir` is the worker directory (`<campaign>/workers/<id>` when the
orchestrator laid the campaign out). Pass that same directory to the driver so
screenshots and video land under `work_dir/media`.

The runner calls `driver.launch()` before any shard action. A launch exception
becomes one finding with `kind="launch"`, severity `critical`, and status
`failed`. Later actions are recorded as skipped and are not sent to the driver.

Each action then maps to one driver call and one `StepResult`:

| Action | Driver call |
| --- | --- |
| `click` | `click(target)` |
| `type` | `type_text(target, text or "")` |
| `key` | `keychord(keys)` |
| `scroll` | `scroll(delta or 0, target)` |
| `menu` | `select_menu(path)` |
| `wait` | `wait_for(target, timeout_s or 5)` |
| `screenshot` | `screenshot(name or "step-<index>")` |
| `assert` | accessibility-tree check, no click |
| `launch` | `launch()` |
| `relaunch` | `relaunch()` |

`assert` with `exists=true` (or `exists` omitted) passes when the query matches
an element and fails when it does not. `exists=false` passes only when the
element is absent. An assertion miss is `kind="assertion"`, not
`missing_control`.

`AppCrashedError`, `UITimeoutError`, and `ElementNotFoundError` fail that step
and open a finding. `explorer.on_step_failure = "stop"` (the default) skips the
remaining actions. `"continue"` keeps going. Any finding sets the shard status
to `failed`. A shard with no findings is `passed`.

On a failed step the runner also takes `failure-<index>.png` when the driver
can still capture the screen. Screenshot actions earlier in the shard stay on
that finding.

## Severity

| Situation | Severity |
| --- | --- |
| Crash, or any launch failure | `critical` |
| Timeout | `high` |
| Missing control, `app.maturity = "prototype"` | `high` |
| Missing control, `app.maturity = "shipped"` | `medium` |
| Assertion | `medium` |

`finding.environment` copies build metadata: `path`, `bundle_id`, and
`version`. `finding.backend` is `config.backend`.

## Video

Recording follows `swarmqa.video_policy`. `always` and `on_failure` start a
recording after a successful launch so the failing path is on tape.
`exploratory_only` does not record a scripted shard. After the shard, the
video file is deleted when `keep_video` is false (`on_failure` and a passed
shard). Kept videos are linked from the finding.

## Paths and files

When `work_dir` lives at `<campaign>/workers/<id>`, screenshot, video, and
replay paths on the finding are relative to `<campaign>`. Otherwise they are
absolute and the work directory is treated as the campaign root.

For each finding the runner writes:

- `findings/<id>.md` via `write_finding`
- `findings/<id>.replay.json` via `write_replay`

The markdown is the built-in issue template filled from the finding (title,
severity, steps, evidence paths, worker id, backend, build version). The replay
file is a version-1 flow (`version`, `name`, `steps`) of the actions that
actually ran, including the failing one. Null fields are omitted so the
document matches `docs/flow.schema.json`. A launch failure replays a single
`launch` step.

## Fingerprints

`fingerprint_for(kind, title, target)` is the first 16 hex characters of
SHA-256 over `kind + "\n" + title.strip().lower() + "\n" + target.strip().lower()`.
`kind` is not case-folded. Scripted findings pass the control label (or the
app path, for launch) as `target`.

## Issue templates

`render_issue` replaces these tokens and leaves every other `{{token}}` as
written. Missing values become empty strings.

`{{title}}` `{{severity}}` `{{kind}}` `{{steps}}` `{{video}}` `{{screenshots}}`
`{{replay_json}}` `{{environment}}` `{{worker_id}}` `{{backend}}` `{{build_id}}`
`{{fingerprint}}` `{{details}}`

`steps` and `screenshots` are newline-joined. `environment` is `key: value`
lines in insertion order. `build_id` is `environment["version"]` or empty.
`default_template()` reads `swarmqa/templates/issue.md`. A missing
`config.issues.template` path falls back to that file.

## Tracker issues

`create_issues` writes the rendered ticket to `findings/<id>.md` (calling
`write_finding` first when that file is not already there) and returns an
`IssueRef(tracker="local", identifier=<finding id>, finding_id=<finding id>)`
for every finding.

GitHub, when `issues.github` is set, runs:

```text
gh issue create --repo <issues.github_repo> --title <title> --body <body>
```

through `runner` (default `subprocess.run(args, capture_output=True, text=True, env=..., check=False)`).
The body contains the video path, screenshot paths, and a fenced replay JSON
stub. When `issues.github_token_env` is set in the environment, that value is
passed to the child as `GH_TOKEN`. The issue URL is read from stdout.

Linear, when `issues.linear` is set, POSTs to `https://api.linear.app/graphql`
via `http_post(url, headers, body) -> dict`. `body` is a dict with `query` and
`variables`. The query calls `issueCreate`. `variables.input` carries
`teamId` (`issues.linear_team`), `title`, and `description` (the same evidence
as the GitHub body). The `Authorization` header is the raw value of
`issues.linear_api_key_env`.

Tracker failures are appended to the finding details and rewritten into the
local ticket. They do not propagate out of `create_issues`. Tokens named by
the GitHub and Linear env settings are never written into campaign files.
