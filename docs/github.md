# GitHub trigger

SwarmQA can test every PR update and every merge into the default branch, then report the findings on the PR. There are two ways to run it:

- **`aqa watch`** on your own Mac polls GitHub with your `gh` login. Nothing to set up on GitHub.
- **GitHub Actions** on a self-hosted Mac runs `aqa run --github-sha` from a workflow. See [integrations/github-action](../integrations/github-action/README.md).

Both build each commit with the per-SHA builder ([build.md](build.md)), run one campaign per platform, print the report, and post it only when `github.report` is set. **Nothing is posted to GitHub by default.**

## aqa watch

```bash
aqa watch                    # poll until Ctrl-C
aqa watch --once             # poll once
aqa watch --pr 12            # test PR #12's head now
aqa watch --latest-pr        # the most recently updated open PR
aqa watch --latest-merged    # the merge commit of the latest PR merged into the branch
aqa watch --dry-run          # print what would be tested; build nothing
```

`--repo owner/name` overrides the repo, `--interval S` the poll interval, and `--config` the config file.

- **Baseline.** The first poll of a repo only records the open PR heads and the branch head. After that, a new or updated PR, or a new commit on the branch, is tested.
- **State.** Tested SHAs are kept in `~/.aqa/state/<owner>-<name>.json` (`$AQA_HOME/state`, or `github.state_root`), so a restart never tests a SHA twice. `--pr` and the other event flags test the event whether or not it was seen.
- **Superseded runs.** One campaign runs per SHA and platform. If a PR's head moves before its run starts, the old SHA is skipped. If it moves during the run, the old SHA still gets its check, but the PR comment is left to the newer run.
- **Cloning.** The builder clones `github.clone_url`, else `build.repo`, else `https://github.com/<repo>.git` (using `gh` as the git credential helper, so private repos work), else `app.source_dir`. GitHub comes before your local checkout because a local checkout usually lacks other people's PR heads.

Exit codes with an event flag: 0 clean, 1 findings or a build failure, 2 could not run.

## aqa run --github-sha

```bash
aqa run --github-sha <sha> [--github-pr N] [--clone-url URL|PATH]
```

Tests one commit the way `aqa watch` does and reports it. Without `--github-pr` it looks up the open PR whose head is that commit; with none, it reports on the commit only. This is what the GitHub Action runs.

## Reporting

| `github.report` | Posts |
| --- | --- |
| `"none"` (default) | nothing; the report goes to stdout |
| `"check"` | a check run on the commit: in progress while testing, then a findings table |
| `"comment"` | one PR comment, edited in place on every run (found by a hidden `<!-- swarmqa:report -->` marker) |
| `"both"` | both |

The check fails on any non-advisory finding; advisory (model-only) findings fail it only with `fail_on_advisory = true`. A platform that could not be built shows as a critical finding with the build log's errors.

**Check runs need a GitHub App token.** GitHub only lets GitHub Apps create check runs. The Actions `GITHUB_TOKEN` is one; a personal `gh` login is not. When the API refuses (HTTP 403), SwarmQA posts a commit status with the same verdict instead, linking to the PR comment.

**Screenshots.** A comment can only show images that are hosted somewhere. With `evidence_command`, SwarmQA runs it once per listed finding with `AQA_FILE` set to the first screenshot, and embeds the last `http…` line it prints. In Actions, the comment also links to the run, whose `swarmqa-reports` artifact holds every screenshot, video and replay.

**Hand-off.** With `handoff = "claude"` or `"codex"`, a comment with failing findings ends by asking `@claude` or `@codex` to fix them on a new branch and open a PR. That only does something if the agent's GitHub app or action is installed on the repo. SwarmQA then tests the fix PR like any other.

## Config: `[github]`

```toml
[github]
repo = "acme/App"            # default: issues.github_repo, $GITHUB_REPOSITORY, then `gh repo view`
# clone_url = "git@github.com:acme/App.git"
platforms = ["ios", "macos"] # default: [app.platform]
events = ["pr", "push"]      # what aqa watch reacts to
# branch = "main"            # default: the repo's default branch
poll_interval_s = 60
report = "none"              # "check", "comment" or "both"
handoff = "none"             # "claude" or "codex"
check_name = "SwarmQA"
fail_on_advisory = false
max_findings = 20
# evidence_command = "./scripts/upload-screenshot.sh"
```

Unknown keys are errors, so a typo cannot quietly turn reporting on or off. Check `gh` with `aqa doctor --github`.

## Intents from the change

With `[flows] from_diff = true` (and `[llm]` enabled), each commit's campaigns also run intents a model proposes from the change, most at risk first. See [flows.md](flows.md).
