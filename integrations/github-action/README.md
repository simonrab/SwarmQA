# SwarmQA in GitHub Actions

Runs SwarmQA on every PR update and every push to the default branch, on a self-hosted Mac, and reports the findings on the PR.

| File | Where it goes |
| --- | --- |
| `action.yml` | stays here; the workflow uses it as `simonrab/swarmqa/integrations/github-action@main` |
| `workflow.yml` | copy to `.github/workflows/swarmqa.yml` in the app repo |

## Setup

1. **Runner.** Register a self-hosted runner on a Mac with Xcode and the simulator runtimes the config needs, labelled `self-hosted` and `macOS` ([GitHub docs](https://docs.github.com/actions/hosting-your-own-runners)). Give it Accessibility permission for macOS runs, and keep its work folder out of iCloud-synced folders such as `~/Documents` and `~/Desktop`. Run `aqa doctor --github` on it once.
2. **Config.** Commit `aqa.config.toml` with `[app]`, intents and a `[github]` table:

   ```toml
   [github]
   platforms = ["ios", "macos"]
   report = "both"        # "check", "comment" or "both"; the default "none" posts nothing
   # handoff = "claude"   # ask @claude (or @codex) in the comment to fix the findings
   ```

   `app.path` is not needed: each run builds the commit with `[build]` (see [docs/build.md](../../docs/build.md)).
3. **Secrets.** Add the model key your `[llm]` table uses (`ANTHROPIC_API_KEY` by default) as a repository secret. Leave it out to run with the built-in checks only.
4. **Workflow.** Copy `workflow.yml` to `.github/workflows/swarmqa.yml` and adjust the branch and `runs-on` labels.

While the SwarmQA repo is private, its Actions settings must allow access from your other repositories (Settings > Actions > General > Access), and `swarmqa-spec` needs a token that can read it, for example `swarmqa[anthropic] @ git+https://x-access-token:${{ secrets.SWARMQA_READ_TOKEN }}@github.com/simonrab/swarmqa@main`.

## What a run does

`aqa run --github-sha <sha> --clone-url $GITHUB_WORKSPACE` builds the commit in a clean checkout under `~/.aqa`, runs one campaign per platform in `github.platforms`, prints the report to the log, and, when `github.report` is set, posts:

- a check run named `SwarmQA` (`github.check_name`) that fails on any non-advisory finding;
- one PR comment, edited in place on every push, with the findings, their steps, the suspected source files and the repro command.

The `reports/` folder is uploaded as the `swarmqa-reports` artifact, and the comment links to the run. The job exits 1 when there are findings or a build failed, and 2 when SwarmQA could not run.

The workflow's `concurrency` group cancels a run when a newer commit arrives on the same PR or branch.

## Inputs

| Input | Default |
| --- | --- |
| `config` | `aqa.config.toml` |
| `sha` | the PR head, or the pushed commit |
| `pr` | the triggering PR |
| `clone-url` | `$GITHUB_WORKSPACE` (check out with `fetch-depth: 0`) |
| `swarmqa-spec` | `swarmqa[anthropic] @ git+https://github.com/simonrab/swarmqa@main` |
| `github-token` | `github.token` (needs `checks: write`, `pull-requests: write`, `statuses: write`) |
| `report-root` | `reports` |

To run on your own Mac without Actions, use `aqa watch` instead (see [docs/github.md](../../docs/github.md)).
