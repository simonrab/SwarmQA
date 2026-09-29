# MCP server

`aqa mcp` runs a stdio MCP server so coding agents (Claude Code, Codex) can start campaigns, read findings and verify fixes. The tools are plain functions in `swarmqa/mcp/tools.py`; `swarmqa/mcp/server.py` registers them with the official `mcp` Python SDK (1.x `FastMCP` or 2.x `MCPServer`).

## Install and register

```sh
uv tool install 'swarmqa[mcp]'          # or: pip install 'swarmqa[mcp]'
```

Without the extra, `aqa mcp` prints `install the extra: uv tool install 'swarmqa[mcp]'` and exits 2.

Claude Code, in the app repository's `.mcp.json`:

```json
{
  "mcpServers": {
    "swarmqa": {
      "command": "aqa",
      "args": ["mcp", "--config", "aqa.config.toml"]
    }
  }
}
```

The same server also starts with `python -m swarmqa.mcp` (use `"command": "python", "args": ["-m", "swarmqa.mcp"]` when `aqa` is not on `PATH`).

Options: `--config` (default `aqa.config.toml`) and `--root` (project directory, default the current directory). Relative paths in tool arguments and in the config (`report_root`, `intents`, `app.path`) resolve against the root.

## Tools

Each tool returns JSON text (the result dataclass through `swarmqa.serialize.to_plain`). Errors an agent can act on (unknown campaign or finding, invalid config, empty intent set) come back as MCP tool errors with a message that says what to do next.

| Tool | What it does |
|---|---|
| `start_campaign(config_path, app, intents, platform, workers, sha)` | Loads the config with `aqa run`-style overrides, builds the queue (bad config or intents fail here), picks the campaign id, and starts the campaign in a detached process. Returns `{campaign_id, report_dir}` at once. `sha` is stored in `mcp.json` for later use. |
| `campaign_status(campaign_id)` | The campaign's `status.json` (latest campaign when omitted). Before the runner writes one, a `running` placeholder. A `running` status whose runner is gone reads as `stopped`. |
| `list_findings(campaign_id, pr, include_advisory, min_severity)` | Summaries from `findings.json`, sorted by severity, then confidence (highest first). While a campaign runs, findings from the worker results written so far. |
| `get_finding(finding_id, campaign_id)` | The full finding (evidence, repro, suspected sources). Without a campaign id, searches the newest campaign first. |
| `verify_fix(finding_id, campaign_id, devices, build)` | Delegates to `swarmqa.verify.start_verify` with the server's config. Returns `state: running` with a `verify_id`. |
| `verify_status(verify_id)` | Delegates to `swarmqa.verify.verify_status`. `passed` means the finding did not reproduce. |
| `cancel_campaign(campaign_id, drain)` | Stops a campaign the server started (see below). |

`pr`: a campaign matches when its `mcp.json` records that PR number. Nothing records a PR yet (the GitHub watcher will), so a `pr` filter returns `[]` today.

## How a campaign runs

`start_campaign` creates `<report_root>/<campaign_id>/` and writes `mcp.json` (request, backend, worker and shard counts, `sha`, runner `pid`). The runner is `python -m swarmqa.mcp._runner <campaign_dir>`, started with `start_new_session=True` in the project root; its stdout and stderr go to `raw/mcp-runner.log`, and it writes `raw/mcp-runner.json` (`exit_code`, `error` when it crashed) when it ends.

The runner hands the chosen id to the orchestrator as `RunOptions(resume_campaign_id=<id>)`. The directory has no `status.json`, so there is nothing to resume and the orchestrator starts a fresh, full campaign under that id.

## Cancelling

- `drain=true` (default) writes `cancel-request.json`. The runner's campaign clock then refuses to schedule new shards; running shards finish, the unstarted ones stay `pending`, and the campaign ends `stopped` with `coverage.stop_reason = "cancelled"`. The orchestrator applies `campaign.on_budget` to this stop like a budget stop, so with `on_budget = "cancel"` running shards are cancelled rather than drained.
- `drain=false` also sends SIGTERM to the runner's process group (SIGKILL after 5 s), then marks `status.json` `stopped` with the interrupted shards under `cancelled`. No summary is written for a hard-cancelled campaign.

Campaigns started with `aqa run` have no runner pid, so `cancel_campaign` refuses them.
