# SwarmQA

Autonomous QA runs campaigns against a macOS app: scripted flows, exploratory hunting, and visual diffs, spread across a small fleet of workers. It writes one merged report with screenshots, session video, replay JSON, and ticket drafts. Human-in-the-loop mode (the default) stops at a draft pull request. Autonomous mode retries failing intents until the run is green or a safety cap hits.

The product plan is in [docs/autonomous-qa-plan.md](docs/autonomous-qa-plan.md). Module contracts are in [docs/CONTRACTS.md](docs/CONTRACTS.md).

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

This tree includes the shared campaign kernel (config model, report layout, spend and budget clocks, fake app driver, CLI). Feature modules raise a clear “not implemented” error until their chunk lands. Off macOS, the driver factory uses the fake driver so tests and dry runs have a session without Accessibility permission.

## Tests

```bash
python -m pytest
```
