# SwarmQA for Codex CLI

This folder registers the `swarmqa` MCP server with Codex CLI and gives Codex the QA loop through `AGENTS.md`.

| File | Where it goes |
| --- | --- |
| `config.toml` | merge into `~/.codex/config.toml` |
| `AGENTS.md` | paste the section into `AGENTS.md` at the app repo root |

## Install

1. Install SwarmQA on the Mac:

   ```bash
   uv tool install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'
   ```

2. In the app repo, run `aqa init`, set `app.path` (or `app.build_command`) and `app.platform`, and run `aqa doctor`.

3. Register the server. Either merge `config.toml` into `~/.codex/config.toml`, or run:

   ```bash
   codex mcp add swarmqa -- aqa mcp
   ```

   Make sure the model key reaches the server (the `env_vars` line in `config.toml`, or an `env` table). Check with `codex mcp list`.

4. Paste `AGENTS.md` into the app repo's `AGENTS.md`, then ask Codex to "QA the app and fix what you find".

The loop is the same as the Claude Code `/qa` skill: `start_campaign`, poll `campaign_status`, `list_findings`, `get_finding`, fix on a branch, `verify_fix`, poll `verify_status`, and open a PR only when the verdict is `passed`.
