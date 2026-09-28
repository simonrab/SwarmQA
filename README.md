# SwarmQA

Autonomous QA runs campaigns against a macOS app: scripted flows, exploratory hunting, and visual diffs, spread across a small fleet of workers. It writes one merged report with screenshots, session video, replay JSON, and ticket drafts. Human-in-the-loop mode (the default) stops at a draft pull request. Autonomous mode retries failing intents until the run is green or a safety cap hits.

The product plan is in [docs/autonomous-qa-plan.md](docs/autonomous-qa-plan.md). Module contracts are in [docs/CONTRACTS.md](docs/CONTRACTS.md). Chunk ownership is in [docs/OWNERSHIP.md](docs/OWNERSHIP.md).

Exploratory hunting defaults to a fast heuristic. Optional smarter backends and advisory UX friction metering:

| Topic | Doc |
| --- | --- |
| Decision backends (heuristic → System One → computer use) | [docs/decision.md](docs/decision.md) |
| UX friction (`friction_path`, gold-relative metrics) | [docs/friction.md](docs/friction.md) |
| Exploratory budgets and findings | [docs/exploratory.md](docs/exploratory.md) |
| Config fields | [docs/config.md](docs/config.md) |

## Install

```bash
python -m pip install -e ".[dev]"
aqa init
```

`aqa init` writes `aqa.config.toml`, `templates/issue.md`, and `intents/` plus `reports/` directories.

## Commands

```bash
aqa run --app /path/to/MyApp.app --intent intents/smoke.md
aqa run --app /path/to/MyApp.app --intent intents/ --backend cloud --workers 12 --max-spend 10 --spend-currency USD
aqa record --app /path/to/MyApp.app --out intents/flow.json --interactive
aqa report
aqa status
aqa baseline update --from-dir reports/<id>/media --baseline-dir baselines
```

Locked defaults: two local workers, human PR mode, video always on. A cloud backend requires `max_spend` greater than zero. The sample cap is 10 USD.

Default `explorer.decision.mode = heuristic` keeps CI on FakeDriver with no model calls. Enable `cascade` (or `system_one` / `computer_use`) only when you have endpoints/commands configured — see [docs/decision.md](docs/decision.md) for privacy notes. Friction findings are advisory under `fail_on = scripted` (`explorer.friction.fail_ci` stays false by default).

Off macOS, the driver factory uses the fake driver so tests and dry runs have a session without Accessibility permission.

## Tests

```bash
python -m pytest
```
