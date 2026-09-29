# SwarmQA for Claude Code

This folder adds a `/qa` skill and the `swarmqa` MCP server to an app repository, so Claude Code can run a SwarmQA campaign, fix what it finds, confirm each fix with `verify_fix`, and open the pull request.

| File | Copy to (in the app repo) |
| --- | --- |
| `skills/qa/SKILL.md` | `.claude/skills/qa/SKILL.md` |
| `.mcp.json` | `.mcp.json` (merge the `swarmqa` entry if the file exists) |

## Install

1. Install SwarmQA on the Mac, with the MCP extra and a model provider:

   ```bash
   uv tool install 'swarmqa[anthropic,mcp] @ git+https://github.com/simonrab/swarmqa'
   ```

   This puts `aqa` on your `PATH` (run `uv tool update-shell` once if it is not).

2. In the app repo, write the config and check the machine:

   ```bash
   aqa init
   aqa doctor
   ```

   Set `app.path` (or `app.build_command`) and `app.platform` in `aqa.config.toml`. Fix anything `aqa doctor` reports as `fail`.

3. Copy the skill and the server entry:

   ```bash
   mkdir -p .claude/skills/qa
   cp /path/to/swarmqa/integrations/claude-code/skills/qa/SKILL.md .claude/skills/qa/
   cp /path/to/swarmqa/integrations/claude-code/.mcp.json .    # or merge by hand
   ```

   Instead of copying `.mcp.json`, you can run `claude mcp add --scope project swarmqa -- aqa mcp`, which writes the same entry.

4. Start Claude Code in the repo. Approve the `swarmqa` project server when asked, and check it with `/mcp`.

The server runs as `aqa mcp` over stdio and inherits Claude Code's environment, so export `ANTHROPIC_API_KEY` (or the variable your `[llm]` table names) in the shell that starts Claude Code.

## Use

```
/qa
/qa intents/checkout.md
```

The skill starts a campaign, waits for it, walks the findings from most to least severe, fixes one at a time on a branch, calls `verify_fix` and polls `verify_status` until the verdict, and opens a PR only for fixes that pass. See `docs/agents.md` in the SwarmQA repo for the tool reference.
