---
name: qa
description: Run a SwarmQA campaign against this iOS or macOS app through the swarmqa MCP server, fix what it finds, confirm each fix with verify_fix, and open a pull request. Use when the user asks to QA, smoke-test or explore the app, to find and fix UI bugs, or to check a SwarmQA finding.
---

# /qa: find, fix and verify app bugs with SwarmQA

SwarmQA runs the app on iOS Simulators or macOS, explores it, and reports findings with screenshots, a video clip, a repro script and suspected source files. You drive it through the `swarmqa` MCP server (`aqa mcp`). You fix the code and open the PR. SwarmQA never edits code or opens PRs.

## When to use

- The user asks to QA, smoke-test or explore the app, or to "find bugs".
- The user names a SwarmQA finding or campaign id and wants it fixed.
- After a UI change, to check the flows it touches.

If the `swarmqa` tools are missing, tell the user to install SwarmQA (`uv tool install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'`), register the server (see `.mcp.json`), and run `aqa doctor`. Do not fall back to guessing.

Arguments, if any (`$ARGUMENTS`), are an intent file or directory, a finding id, or a campaign id.

## The loop

1. **Start.** Call `start_campaign` with `config_path` (default `aqa.config.toml`) and, when relevant, `intents`, `platform` (`ios` or `macos`), `workers` and `sha` (the current `git rev-parse HEAD`). It returns `campaign_id` at once. Skip this step when the user gave a campaign or finding id.
2. **Wait.** Poll `campaign_status(campaign_id)` about every 30 seconds until `state` is `finished` or `stopped`. Tell the user the queue depth and spend now and then; do not poll in a tight loop.
3. **Triage.** Call `list_findings(campaign_id, include_advisory=True, min_severity="low")`. Work in severity order (`critical`, `high`, `medium`, `low`). Findings with `advisory: true` came only from a model: show them to the user and fix them only if the user agrees or the evidence is clear.
4. **Read one finding.** Call `get_finding(finding_id, campaign_id)`. Look at the steps, screenshots, video clip, `repro` script and `suspected_sources` (repo-relative `path` or `path:line`). Open those files first.
5. **Fix.** Work on a branch (`git switch -c qa/<finding-id>`), never on the default branch. Make the smallest change that fixes the cause. Run the project's own unit tests if it has them.
6. **Verify.** Call `verify_fix(finding_id, campaign_id)`. It rebuilds the app and replays the repro on 2 devices, and returns at once with a `verify_id` and state `running`.
7. **Wait for the verdict.** Poll `verify_status(verify_id)` about every 20 seconds until `state` is not `running`.
   - `passed`: the finding did not reproduce on any device. Go on to step 8.
   - `failed`: it still reproduces on `reproduced_on`. Read `evidence` and `message`, change the fix, and verify again. Stop after 3 failed attempts and report to the user.
   - `error`: the build or replay could not run. Report `message` to the user; do not claim the fix works.
8. **Open the PR.** Commit, push, and open one PR per finding (or one per related group) with `gh pr create`. In the body, include the finding id and title, the root cause, the fix, and the `verify_fix` result (state, devices, evidence paths). Never merge it and never push to the default branch.

Repeat steps 4 to 8 for the next finding. When done, summarise: fixed and verified (with PR links), not fixed (with the reason), and advisory findings left for the user.

## Rules

- Only a `passed` verify_status counts as fixed. Say so plainly when a fix is unverified.
- Use `cancel_campaign(campaign_id)` if the user asks to stop; it drains running shards by default.
- Do not edit `aqa.config.toml`, spend caps or worker counts unless the user asks.
- Do not commit anything under `reports/` or `.aqa/`.
- Never print API keys or tokens.
