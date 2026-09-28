# Chunk ownership

Cloud agents implement one chunk at a time on top of `cursor/aqa-foundation-d827`.
Edit only the files listed for your chunk. Shared modules already implement
campaign types, spend and budget clocks, report layout, summary format,
finding dedup, the fake app driver, video policy, and the CLI dispatcher.

Pull requests target `cursor/aqa-foundation-d827`.

| Chunk | Owns |
| --- | --- |
| C1 | `swarmqa/config.py`, `tests/test_config.py`, `docs/config.md` |
| C2 | `swarmqa/driver/macos.py`, `tests/test_macos_driver.py`, `docs/driver.md` |
| C3 | `swarmqa/intent/ingest.py`, `swarmqa/intent/record.py`, `tests/test_intent.py`, `docs/intents.md` |
| C4 | `swarmqa/explorer/scripted.py`, `swarmqa/reporter/findings.py`, `swarmqa/reporter/issues.py`, `tests/test_scripted.py`, `tests/test_reporter.py`, `docs/reporter.md` |
| C5 | `swarmqa/explorer/exploratory.py`, `tests/test_exploratory.py`, `docs/exploratory.md` |
| C12 | `swarmqa/decision/` (`protocol`, `heuristic`, `system_one`, `cascade`, `cache`, `providers`, …), `tests/test_decision.py`, `docs/decision.md`; extends C5 observe→decide→act |
| C13 | `swarmqa/friction/` (`session`, `score`, `gold`, `emit`, `personas`), `tests/test_friction.py`, `docs/friction.md`; hooks in C5 exploratory; extends report summary + `[explorer.friction]` config |
| C6 | `swarmqa/visual/diff.py`, `swarmqa/visual/baseline.py`, `tests/test_visual.py`, `docs/visual.md` |
| C7 | `swarmqa/prloop/loop.py`, `tests/test_prloop.py`, `docs/agents.md` |
| C8 | `swarmqa/orchestrator/campaign.py`, `swarmqa/orchestrator/status.py`, `swarmqa/backends/local.py`, `swarmqa/worker.py`, `tests/test_orchestrator.py`, `docs/orchestrator.md` |
| C9 | `swarmqa/backends/tart.py`, `swarmqa/backends/cloud.py`, `tests/test_backends.py`, `docs/backends.md` |
| C10 | `swarmqa/driver/ios.py`, `tests/test_ios_driver.py`, `docs/driver.md`; `app.platform` / `app.simulator` / `app.simulators` on `AppTarget` |

Do not edit `swarmqa/cli.py`, `swarmqa/errors.py`, `pyproject.toml`, or another chunk's files unless your chunk row lists them. Shared types live in `swarmqa/models.py` — extend them only when the field is campaign-wide (C12/C13 did this for `decision` / `friction` / `friction_path`; C10 did this for `app.platform`); keep chunk-local types local otherwise. If a signature in your stub cannot express the behavior, keep the signature and add optional keyword-only arguments.

Construct `CampaignConfig` in tests with `swarmqa.testing.sample_config`. Call `swarmqa.config.load_config` only from C1 tests.

Web targets (C11) are out of this delivery.
