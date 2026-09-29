<!-- Paste this section into AGENTS.md at the root of the app repository. -->

## QA with SwarmQA

This repo is tested with SwarmQA through the `swarmqa` MCP server (`aqa mcp`). SwarmQA runs the app on iOS Simulators or macOS and reports findings. You fix the code and open the pull request; SwarmQA never edits code.

Use it when asked to QA, smoke-test or explore the app, to find UI bugs, or to fix a SwarmQA finding. If the `swarmqa` tools are missing, ask the user to install SwarmQA and run `aqa doctor`.

1. `start_campaign(config_path="aqa.config.toml", sha=<git rev-parse HEAD>)` returns a `campaign_id` at once. Skip it when the user gave a campaign or finding id.
2. Poll `campaign_status(campaign_id)` about every 30 seconds until `state` is `finished` or `stopped`.
3. `list_findings(campaign_id)`. Work from `critical` down to `low`. `advisory: true` findings came only from a model: fix them only if the user agrees or the evidence is clear.
4. `get_finding(finding_id, campaign_id)`. Read the steps, screenshots, video clip, `repro` script and `suspected_sources`; open those files first.
5. Fix on a branch (`qa/<finding-id>`), never on the default branch. Keep the change small and run the project's tests.
6. `verify_fix(finding_id, campaign_id)` rebuilds and replays the repro on 2 devices. It returns a `verify_id` at once.
7. Poll `verify_status(verify_id)` about every 20 seconds until `state` is not `running`. `passed` means fixed. `failed` means it still reproduces: read `evidence`, change the fix, verify again, and stop after 3 attempts. `error` means the build or replay could not run: report it.
8. Only after `passed`: commit, push, and `gh pr create` with the finding id, root cause, fix and the verify result. Never merge and never push to the default branch.

Do not edit `aqa.config.toml` or its spend caps unless asked, do not commit `reports/` or `.aqa/`, and never print API keys or tokens. `cancel_campaign(campaign_id)` stops a campaign.
