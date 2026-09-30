# Build plan: an agent-native iOS + macOS QA swarm

This is the phased plan for taking SwarmQA from a solid orchestrator to a swarm that watches GitHub, builds each PR, drives the build on many simulators or VMs, and hands findings to Claude Code or Codex to fix. `docs/autonomous-qa-plan.md` holds the product intent. This file holds the build order.

## Where we started

The orchestrator (scheduling, budgets, resume, dedup, reports) is solid. Little else worked end to end:

- **No model integration.** The "model" stages are shell and URL hooks.
- **Exploration is single-screen.** The tree is read once (`explorer/exploratory.py`) and decisions are keyword matching (`decision/heuristic.py`).
- **Computer use can never run.** No screenshot is ever passed to it.
- **The drivers are too slow.** AppleScript `entire contents` walks on Mac, and idb on iOS with likely-wrong argument order.
- **Tart is broken.** It doesn't wait for boot, and a missing result counts as a pass (`backends/tart.py`).
- **The PR loop was unsafe.** It could not be turned off, and it branches and runs `git add --all` in the user's working copy.
- **Nothing has been validated** against a real simulator or VM.

## Decisions

- Both platforms are built in parallel.
- Claude and OpenAI adapters are both built in.
- No real app is baked in. An in-repo fixture app with planted bugs is the benchmark. A private real app may be used for local validation only, configured in gitignored files, and **nothing about it is ever committed**.
- The visual-judge plumbing is committed in Phase 0. Phase 3 swaps its shell-command backend for the model providers and keeps `command` as a fallback.
- **Languages: Python for the brain, Swift for the hands.** Orchestration, model calls, MCP, GitHub, reports and scheduling stay in Python: the work is I/O-bound, the orchestrator and its tests already exist, and the Anthropic, OpenAI and MCP SDKs are strongest there. The on-device runner and the fixture app are Swift, because XCUITest only runs in Swift. Rust was considered and rejected for now: it cannot replace the Swift runner, the speed gain would not show in simulator-bound work, and it would mean a rewrite. Revisit it only for the Phase 7 host service.
- **Distribution:** `uv tool install git+https://github.com/simonrab/swarmqa` (or `pipx`), so users never touch the macOS system Python. CI checks the tool install on every push.

## How the work is split across subagents

- **The lead (main session) owns contracts, merges and gates.** Each work package (WP) goes to exactly one `general-purpose` subagent running with `isolation: "worktree"`, launched in the background.
- **At most 4 subagents run at once.** Every wave starts only after its dependencies have merged.
- **Each WP owns a disjoint set of paths.** Contract files from Phase 1 are read-only for subagents. A subagent that needs a contract change reports it back and the lead makes it.
- **Every WP prompt includes:** the goal; the paths it owns and the contract files it may read; the acceptance tests it must add and pass; "Do not commit; report a summary plus any open issues"; "Never add references to the private validation app to tracked files."
- **Gate after each wave, run by the lead:**
  1. Merge the worktrees.
  2. Run `pytest` (Ubuntu and macOS CI).
  3. Run `/code-review`.
  4. From Phase 3 on, run the planted-bug benchmark (see Verification).
  5. Before committing, check the staged diff has no references to the private validation app.
  6. One commit or PR per wave.
- **Expensive or ambiguous research** goes to an `Explore` subagent first, for example XCUITest remote-control patterns, `mcp` SDK usage, and Tart guest-agent readiness.

## Phase 0: Stabilise (lead only) — done

1. Commit the visual-judge work.
2. Make the PR loop safe: `pr.mode = "off"` is the default and skips the fix loop.
3. Add `driver.kind` (`auto | fake | legacy | runner`); `create_driver` honours it and `fake` gives a dry run on a Mac.
4. Run pytest on `macos-latest` as well as `ubuntu-latest`.
5. Gitignore local-only targets: `.aqa/` and `aqa.local.toml`.

## Phase 1: Contracts (lead, sequential, before any fan-out) — done

Define these interfaces with fakes so the Phase 2 and 3 subagents can build against them in parallel:

| Contract | File | Notes |
|---|---|---|
| Driver v2 | `swarmqa/driver/protocol.py` | Adds `observe() -> Observation(tree, screenshot, ts)` as one round-trip, `tap_point`, `swipe`, `logs_since(ts)`, `crash_reports_since(ts)`. Keeps the existing methods. |
| Runner wire protocol | `agents/swarm-runner/PROTOCOL.md` + `swarmqa/driver/runner_schema.py` | HTTP/JSON: `/health`, `/launch`, `/tree`, `/observe`, `/tap`, `/type`, `/swipe`, `/key`, `/screenshot`. Shared by Swift and Python. |
| ModelProvider | `swarmqa/llm/protocol.py` | `decide_step(obs, goal, history)`, `judge_screen(screenshot, tree, rubric)`, `propose_flows(diff, files)`, `computer_use(screenshot, instruction)`. Structured outputs and a usage/cost report. |
| DevicePool | `swarmqa/devices/protocol.py` | `acquire(platform, build) -> Device`, `release`, `capacity()`. |
| Findings v2 | `swarmqa/models.py` + `schemas/findings.v2.json` | Adds `category` (broken/visual/confusing/crash), `confidence`, `advisory`, `repro`, `suspected_sources[]`, `evidence{video_clip, frames[]}`. |
| MCP tool signatures | `swarmqa/mcp/tools.py` (stubs) | `start_campaign`, `campaign_status`, `list_findings`, `get_finding`, `verify_fix`, `cancel_campaign`. |

Update `driver/fake.py` and the fake decision provider to the new contracts.

## Phase 2, Wave A: foundations (4 subagents) — done

- **WP-A1: Swift runner.** Owns `agents/swarm-runner/**`. An Xcode project with iOS and macOS UI-test targets. Each hosts a small HTTP server inside the XCUITest process and drives any app through `XCUIApplication(bundleIdentifier:)`, serialising `snapshot()` to the protocol and handling taps, typing, swipes and screenshots. Build script `agents/swarm-runner/build.sh`. Acceptance: builds for both platforms with `xcodebuild`; `/observe` on the fixture app takes under 300 ms. **Done:** iOS verified end to end, with `/observe` on the fixture at p50 112 ms and p95 217 ms (PNG). macOS verified end to end on the fixture (launch, taps, typing, keys, swipe, crash detection); its `/observe` is over budget because it captures the whole display (JPEG p50 270 ms, p95 355 ms at load 5; tree only p50 104 ms). The first run needs the user to approve automation mode.
- **WP-A2: Planted-bug fixture app.** Owns `fixtures/PlantedBugs/**`. A multiplatform SwiftUI app with bugs tagged by accessibility identifier: a dead button, clipped text, overlapping views, a crash on one path, an endless spinner, a confusing multi-step settings flow, a missing label, and a low-contrast element. Includes `bugs.json` (ground truth) and a `build.sh` that produces simulator and macOS `.app` files. **Done:** 8 bugs, checked by `check_bugs.py`.
- **WP-A3: Model providers (done).** Not yet run against the live APIs; OpenAI prices and model names still to confirm; fixtures are hand-written and should be re-recorded. Owns `swarmqa/llm/**` and the optional `anthropic` and `openai` extras. Anthropic adapter (a small fast model for steps, a larger model for judgments), OpenAI adapter, fake adapter, prompts in `swarmqa/llm/prompts/`. JSON-schema structured outputs, retries and timeouts. Token cost goes into `swarmqa/spend.py`. Tests use record/replay fixtures, not live calls.
- **WP-A4: Device pools (done).** The simctl commands were checked on a real Mac (see `docs/devices.md`); the tart ones still need a host with Tart. Owns `swarmqa/devices/**` and `swarmqa/backends/tart.py`. iOS: clone a golden simulator per worker, boot and wait on bootstatus, install the build, erase on release, host-wide locks in `~/.aqa/locks`, capacity from RAM and cores. Tart: poll `tart exec … true` for readiness, treat a missing `result.json` as an error, use the correct campaign directory, copy with `symlinks=True`, enforce the 2-VM-per-host cap. Reuse the command-runner seam.

## Phase 3, Wave B: the QA loop (4 subagents) — done; checks need tuning against real runs (see WP-B3)

- **WP-B1: Runner drivers.** Owns `swarmqa/driver/runner_client.py`, `ios_runner.py`, `macos_runner.py`. Driver v2 over the runner protocol, installing and launching the runner in the simulator or VM. The old drivers remain as `driver.kind = "legacy"`. Needs A1 and A4. **Done:** `driver.kind = "runner"`, verified live on an iOS simulator and the Mac against the fixture.
- **WP-B2: Explorer v2 (done).** Owns `swarmqa/explorer/agent_loop.py` and `swarmqa/explorer/screen_graph.py`. An observe-decide-act loop with a fresh observation every step. Screen fingerprints build a screen graph. Goal mode follows an intent; crawl mode visits untried controls breadth-first until coverage or budget runs out. Uses the ModelProvider with `decision/heuristic.py` as the zero-cost first try.
- **WP-B3: Checks (done).** Owns `swarmqa/checks/**`. `functional.py` (crashes, hangs, dead taps, error alerts, console errors), `layout.py` (truncation, overlap, off-screen, tap targets under 44pt, missing labels, contrast), `baseline.py` (vectorised diff, flag size mismatches instead of resizing), `judge.py` (visual judge on the ModelProvider; model-only findings are `advisory` with a confidence), `friction.py` (fix the double-counted KLM term, fill `Shard.tags` during ingest).
- **WP-B4: Evidence and findings v2 (done).** Owns `swarmqa/report/**` and `swarmqa/reporter/findings.py`. A repro replay script per finding, a trimmed video clip around the failure (ffmpeg optional), `findings.json` v2 and `summary.md`, `source_map.py` (accessibility identifiers to source files via `git grep`), and a cross-run dedup store in `.aqa/state/`.

**Gate:** the planted-bug benchmark on 2 iOS simulators and 1 macOS VM (or the local Mac) runs end to end with the Anthropic provider, and again with OpenAI.

## Phase 4, Wave C: agent interface, builder and hand-off (4 subagents) — C1, C2 and C3 done; C4 (builder) not started

- **WP-C4: Builder.** Owns `swarmqa/build/**`. Checks out a SHA into `~/.aqa/checkouts/<repo>/<sha>`, never touching the user's working copy. Builds the iOS simulator and macOS `.app` from `build.ios.command` / `build.macos.command` or by auto-detecting with `xcodebuild -list`. DerivedData per repo. Returns `BuildArtifact(sha, platform, app_path, bundle_id, log_path)`. A build failure is a critical finding with the log tail. **Done:** `swarmqa/build`, `aqa build --sha`, a `[build]` config table and the verify wiring. Checked live: a cold build of the fixture takes about 5 minutes per platform on a loaded 8 GB Mac, a warm rebuild about 5 s.
- **WP-C1: MCP server (done).** Owns `swarmqa/mcp/**`, the optional `mcp` extra and `aqa mcp`. Stdio transport. `start_campaign` launches a detached campaign and returns an id right away. Status comes from `orchestrator/status.py`.
- **WP-C2: verify_fix and replay (done).** Owns `swarmqa/verify/**`. Builds, replays a finding's repro across N devices, returns pass or fail with evidence.
- **WP-C3: Integrations, packaging and cleanup (done).** Owns `integrations/claude-code/` (a `/qa` skill plus an `.mcp.json` snippet), `integrations/codex/`, and `aqa doctor` (Xcode, runtimes, permissions, API keys, tart). Deletes `swarmqa/prloop/` because the coding agent opens PRs. Moves `reporter/issues.py` behind `aqa file-issues`. Keeps `uv tool install` working with the optional extras (`uv tool install 'swarmqa[anthropic,mcp] @ git+…'`) and documents it.

**Gate:** from Claude Code, run `/qa` on the fixture app, fix one planted bug, call `verify_fix`, and see it pass. Repeat from Codex CLI.

## Phase 5, Wave G: GitHub trigger (3 subagents)

- **WP-G1: GitHub watcher.** Owns `swarmqa/github/watch.py`, `swarmqa/github/client.py`, `aqa watch`. Uses `gh api` with the user's existing auth. Polls for opened or updated PRs and default-branch merges; supports `--pr N`, `--latest-pr`, `--latest-merged` and continuous watching. Remembers processed SHAs in `~/.aqa/state/<repo>.json`. One campaign per SHA; newer SHAs supersede older ones of the same PR.
- **WP-G2: Reporting back to GitHub.** Owns `swarmqa/github/report.py`. Off by default; opt-in via `github.report = "check" | "comment" | "both"`. A check run with a findings table, and one updated-in-place PR comment with screenshots and the repro command. Evidence goes to workflow artifacts or a configured bucket. Optional `github.handoff = "claude" | "codex" | "none"`.
- **WP-G3: CI packaging.** Owns `integrations/github-action/**`. A reusable workflow for a self-hosted macOS runner on `pull_request` and default-branch `push`, running `aqa run --github-sha $SHA`. Setup docs, and `aqa doctor --github` for `gh` scopes.

**Gate:** on a scratch repo containing the fixture, `aqa watch` detects a PR, builds it, runs 3 simulators plus the macOS app, and posts a check run and a comment. The hand-off produces a fix PR, a re-run clears the finding, and merging triggers the post-merge run.

## Phase 6, Wave D: flows and speed (3 subagents)

- **WP-D1: Flows from the diff.** Owns `swarmqa/flows/from_diff.py`. The diff and touched SwiftUI files go to `propose_flows`, which returns prioritised intents in the existing intent format.
- **WP-D2: Replay cache and self-heal.** Owns `swarmqa/flows/cache.py`. Successful explorations are saved to the target repo's `.aqa/flows/` and replayed first; a failing step is re-explored alone and the script updated.
- **WP-D3: Swarm scheduler.** Owns changes to `swarmqa/orchestrator/campaign.py`. Pulls devices from DevicePools, splits the crawl frontier across devices, merges screen graphs across the campaign.

**Target:** 20 flows on 6 iOS simulators in under 10 minutes on cached runs.

## Phase 7, Wave E: scale out (2 subagents)

- **WP-E1: Runner service.** Owns `swarmqa/server/**`. An HTTP API on each Mac host with token auth, a host registry and artifact upload. The one place where Rust is worth re-evaluating, if Python proves painful for a long-running daemon.
- **WP-E2: Remote client.** Adds `--remote` to the CLI and MCP so cloud agents and CI work without a local Mac. `aqa watch` can dispatch to a pool of Mac hosts.

## Verification

- **Unit tests:** `pytest` on Ubuntu and macOS CI, with fakes and model-response fixtures.
- **Planted-bug benchmark:** `aqa bench fixtures/PlantedBugs` builds the fixture, runs both platforms, and scores findings against `bugs.json`. Targets: at least 90% of planted bugs found, at most 2 non-advisory false positives, wall time reported. Run at every gate from Phase 3 on.
- **GitHub end-to-end:** the Phase 5 gate.
- **Agent round-trip:** Claude Code and Codex CLI, through MCP.
- **Real-app check:** run against a private app using config in `.aqa/local/` or `aqa.local.toml`, both gitignored. Nothing from this run is committed.
- **Tart:** manual on a real Mac host, since GitHub runners can't nest VMs. Script: `scripts/tart-smoke.sh`.
