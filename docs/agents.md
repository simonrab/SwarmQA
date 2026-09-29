# Coding agents

SwarmQA finds bugs; a coding agent fixes them. Claude Code and Codex drive SwarmQA through its MCP server, read the findings, change the code, ask SwarmQA to confirm the fix, and open the pull request themselves. SwarmQA never edits product code, never opens or merges a pull request, and does not pick a model vendor for the agent.

The built-in fix and PR loop (`--pr-mode`, `[pr]`, `swarmqa/prloop/`) was removed. A config that still has a `[pr]` table loads, prints a deprecation warning on stderr, and its contents are ignored.

## Setup

Install with the `mcp` extra and a model provider, then check the machine:

```bash
uv tool install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'
aqa init        # in the app repo
aqa doctor
```

Register the server, which runs as `aqa mcp` over stdio:

| Agent | Files |
| --- | --- |
| Claude Code | `integrations/claude-code/`: `.mcp.json` (`{"mcpServers": {"swarmqa": {"command": "aqa", "args": ["mcp"]}}}`) and the `/qa` skill at `.claude/skills/qa/SKILL.md` |
| Codex CLI | `integrations/codex/`: `[mcp_servers.swarmqa]` in `~/.codex/config.toml` and a section for the repo's `AGENTS.md` |

The server inherits the agent's environment, so the model key your `[llm]` table uses (`ANTHROPIC_API_KEY` by default) must be set where the agent starts.

## Tools

| Tool | Returns | Notes |
| --- | --- | --- |
| `start_campaign(config_path, app, intents, platform, workers, sha)` | `campaign_id`, `report_dir` | Returns at once; the campaign runs in a detached process |
| `campaign_status(campaign_id)` | `CampaignStatus` | `state` is `running`, `finished` or `stopped`; `None` means the latest campaign |
| `list_findings(campaign_id, pr, include_advisory, min_severity)` | finding summaries | `advisory` findings came only from a model |
| `get_finding(finding_id, campaign_id)` | the full finding | steps, screenshots, video clip, `repro`, `suspected_sources` |
| `verify_fix(finding_id, campaign_id, devices=2, build=True)` | `verify_id`, state `running` | Rebuilds and replays the repro; returns at once |
| `verify_status(verify_id)` | `VerifyResult` | `passed` means the finding did not reproduce on any device |
| `cancel_campaign(campaign_id, drain=True)` | `CampaignStatus` | `drain` lets running shards finish |

## The loop

1. `start_campaign`, then poll `campaign_status` until the campaign is `finished` or `stopped`.
2. `list_findings`, most severe first. Confirm advisory findings with the user before fixing them.
3. `get_finding`, then open the `suspected_sources` and read the evidence.
4. Fix on a branch, never on the default branch.
5. `verify_fix`, then poll `verify_status` until the state is not `running`.
   - `passed`: commit, push and open the PR with the finding id, the cause, the fix and the verify result.
   - `failed`: the finding still reproduces on `reproduced_on`. Revise and verify again; stop after 3 attempts and report.
   - `error`: the build or replay could not run. Report it; the fix is unverified.
6. Repeat for the next finding, then summarise what was fixed, verified and left open.

## Rules for agents

- Only a `passed` verify result counts as fixed.
- Open one PR per finding or per related group. Never merge and never push to the default branch.
- Do not change `aqa.config.toml`, spend caps or worker counts unless the user asks.
- Do not commit `reports/` or `.aqa/`.
- Never print API keys or tokens.

## Without MCP

Every step also works from a shell: `aqa run`, `aqa status`, `aqa report`, and `aqa replay reports/<campaign-id>/findings/<id>.replay.json` (exit 1 while the finding still reproduces). `aqa file-issues` files a campaign's findings into GitHub or Linear for people to pick up; it is a dry run unless you pass `--yes`.
