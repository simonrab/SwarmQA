---
name: explore
description: Explore this iOS or macOS app yourself on one or more simulators through the swarmqa MCP server, choosing every tap, and report bugs, broken flows and UI problems with screenshots. Use when the user asks you to test, explore or QA the app or a pull request by hand, or to "click through" a flow.
---

# /explore: drive the app yourself with SwarmQA

You are the tester. SwarmQA gives you the devices and the evidence: it launches the app, carries out your actions, runs its automatic checks after every one, and writes a report with screenshots, video and replays. You drive it through the `swarmqa` MCP server (`aqa mcp`).

Arguments (`$ARGUMENTS`), if any, are the goal, a PR number, or a device count.

## Steps

1. **Open.** Call `open_sessions(goal=..., devices=N)`. Write the goal as what to explore and what to avoid (for example "Explore sign-in and onboarding; do not create an account or sign in"). Add `pr=<number>` to test a pull request's build. Simulators can take minutes to boot; if a session is `starting`, call `observe` with `wait_s=120`.
2. **Several devices.** With more than one session, start one subagent per session and give each the session id, the goal and these steps; each explores a different part of the app.
3. **Explore.** Loop: `observe(session_id)`, read the `tree` and open the `screenshot` image, decide, then `act(session_id, kind=..., element_id=...)`. Element ids change with every screen; use the latest ones. Use `back` to leave a screen. Dismiss alerts before anything else.
4. **Report.** After each action read `new_findings`: those are the automatic checks. Call `report_issue` for anything else a careful tester would flag: wrong or misleading text, clipped or overlapping layout, a control that does the wrong thing, a confusing flow. Title it as the defect. Do not report environment problems (no Apple ID on the Simulator, no network).
5. **Finish.** `end_session` for each session. Then `list_findings(campaign_id, include_advisory=True)` and summarise for the user: what you covered, each finding with severity and screenshot path, and what you could not reach.

## Rules

- Follow the goal's limits. Never enter real personal data, passwords or payment details; never create accounts unless the user says so.
- Do not edit `aqa.config.toml` unless the user asks.
- If the `swarmqa` tools are missing, tell the user to install SwarmQA with the `mcp` extra and register the server (see `.mcp.json`).
