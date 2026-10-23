# System One benchmark: Laya on PlantedBugs

What happened when the agent explorer took its steps from a System One model (`provider = "system_one"`, see `docs/llm.md`) on the PlantedBugs fixture. Runs from 22 Oct 2026 on an 8 GB M1 Mac, iOS Simulator, runner driver.

## Setup

- **Model.** Laya, the only Jev-compatible model that fits next to a simulator in 8 GB of RAM: a 0.4B ModernBERT classifier served by `laya-serve` on `127.0.0.1:8000`, on the CPU, using about 580 MB. Hosted Jev was not tested: no `TYPESAFE_API_KEY` was set.
- **Campaign.** 4 workers: the scripted "Open the scanned attachment" shard, and the goals "Open the scanned attachment", "Turn on cloud sync" and "Share a task" in the agent explorer.
- **Baseline.** `claude_cli` (Claude through `claude -p`) on the same app: `reports/20261022T140010Z-778f89`, 2 goal workers.

## Results

| Run | Steps from | Step latency | Confident answers | Planted bugs found | Goals reached |
| --- | --- | --- | --- | --- | --- |
| Baseline, `claude_cli` | Claude, rules when it fails | about 20–30 s | n/a | 5 of 8 (no scripted shard) | 0 of 2 |
| (a) Laya, rules fallback, `done` bar 0.5 | Laya, rules when unsure | p50 1.3–4.6 s; 13 of 26 calls timed out at 5 s | 5 of 26 | 6 of 8 | 0 of 3; Laya said `done` early 3 times |
| (a′) Laya on CPU, `done` bar 0.8, 10 s timeout | Laya, rules when unsure | p50 0.75 s, p95 up to 8.9 s; no timeouts | 11 of 35 | 5 of 8 | 0 of 3; Laya said `done` early once, at 0.84 |
| (b) Laya, escalating to `claude_cli` | not valid | | | | |

Run (b) (`reports/20261022T213121Z-b3d656`) does not count. `claude -p` had hit its usage limit, so every escalation failed and the steps came from the rules.

The planted bugs found in (a) and (a′) came from the checks on screens that the rules and the fallback crawl reached, as in the baseline. The crash came from the scripted shard. No bug was found because of a step that Laya chose.

**Triage.** 45 findings were triaged. Laya's confidence was never above 0.27, below the 0.5 bar, so triage changed no finding. It called 43 of them `real_bug`.

## Conclusions

- Laya is fast enough: about 0.7 s per step on the CPU, against 20–30 s for `claude -p`. But it is rarely sure, and when it is sure that a goal is done, it is wrong. It is not good enough to explore on its own.
- The provider fails safe. Unsure answers go to the rules, and nothing crashed.
- A wrong `done` ends the goal, and a higher bar did not stop it (0.84 passed a 0.8 bar). So `trust_done` is now off by default: a `done` from System One goes to `escalate_to` to confirm, or is refused and the rules carry on.
- Still to measure: hosted Jev (needs `TYPESAFE_API_KEY`); escalation to a large model (needs an API key, because `claude -p` on a subscription is now turned off for this organization); a larger open model (kev-4b and decider-2b need more than 8 GB of RAM).

## Reproduce

```bash
~/.venvs/jev/bin/laya-serve &          # pip install 'laya[serve]' into ~/.venvs/jev
aqa run --config planted-system-one.toml
```

`planted-system-one.toml` is the PlantedBugs config with `[llm] provider = "system_one"` and the `[llm.system_one]` table from `docs/llm.md`. Each worker writes `workers/<id>/system_one.json` with call counts, escalations, latency and triage verdicts.
