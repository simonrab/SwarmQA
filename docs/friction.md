# UX friction (advisory)

SwarmQA can meter exploratory hunts like a confused or expert user and emit
advisory `friction_path` findings when a goal-directed path is costly relative
to a gold path. This is **numbers**, not LLM taste: step ratios, backtracks,
recovery loops, dead ends, and optional KLM-style length ratios.

Package: `swarmqa/friction/`. Hooked from `swarmqa.explorer.exploratory`
after each successful accessibility-tree read, click, and menu (and on failed
clicks/menus for dead-end counters). Ownership: **C13** in `docs/OWNERSHIP.md`.

## When a finding appears

Emit only when all of these hold:

1. Friction is enabled (`explorer.friction.enabled`, default true).
2. The hunt is goal-directed (has a goal / intent locus).
3. Score ≥ `emit_threshold` (default **50**). If gold is **synthesized**
   (no scripted sibling / provided gold), the floor is **65**.
4. At least one structural signal:
   - `steps_observed - steps_gold ≥ min_extra_steps` (default **3**), or
   - `dead_end_count ≥ 1`, or
   - `recovery_loops ≥ 1`, or
   - `backtrack_rate ≥ min_backtrack_rate` (default **0.15**) *and* the hunt
     also has extra steps or dead-end/recovery pathology (short happy paths
     stay silent).

Titles always lead with a number, for example `3.2× gold path (score 72)` or
`4 backtracks (score 55)`.

## Gold path

`resolve_gold` prefers, in order:

1. Explicit `gold_steps`
2. Scripted sibling step count — exploratory shard `actions`, or tags
   `gold_steps:N` / `gold:N`
3. Shortest recorded success (when provided by the caller)
4. Synthesized: `max(1, expected_controls)` from the goal text

Gold-relative scoring avoids “N clicks = friction” false positives on short
FakeDriver happy paths. Existing exploratory tests stay quiet because a
one-click Save hunt observes about two steps against synthesized gold of 1,
which is below `min_extra_steps = 3`.

## Score (0–100)

```text
score =
  35 * clip((step_ratio - 1) / 2, 0, 1) +
  20 * clip(backtrack_rate / 0.25, 0, 1) +
  15 * clip(recovery_loops / 3, 0, 1) +
  15 * clip(dead_end_count / 2, 0, 1) +
  10 * clip((klm_ratio - 1) / 2, 0, 1) +
  5  * clip(rage_events / 2, 0, 1)
```

`step_ratio = steps_observed / steps_gold`. When `klm = true`, `klm_ratio`
tracks the same length ratio as a simple operator-act proxy. Session counters
also track unique state hashes (window labels + sorted actionable
role/label/enabled), revisits, rage clicks (same target ≥3× with no state
change), dead clicks, stall length, choice entropy, and actionable counts.

## Personas

`explorer.friction.personas` defaults to `["expert", "first_time"]`. The hunt
records the first persona on `FrictionSession` (policy hints in
`swarmqa.friction.personas`). Decision backends may consume those hints later;
metering works without them.

| Persona | Hint |
| --- | --- |
| `first_time` | Prefer visible labels; at most one wrong-label pick then recover; prefer Back/Cancel on dead ends |
| `expert` | Prefer menus / gold-biased shortcuts |

## Config

```toml
[explorer.friction]
enabled = true
emit_threshold = 50
min_extra_steps = 3
min_backtrack_rate = 0.15
personas = ["expert", "first_time"]
fail_ci = false
compare_to = "gold"   # gold | prior_p50
klm = true
```

### Advisory posture and `fail_ci`

`fail_ci` defaults to **false** and is **reserved** — it is **not** wired to
`fail_on` yet. Do not rely on it to fail CI.

Under the default `fail_on = scripted`, exploratory-only findings (including
`friction_path`) already leave process exit **0**. The orchestrator only fails
the process for scripted/suite shard failures (or errors) in that mode.
`fail_on = any` still treats any finding — including friction — as exit 1.

## Report

`summary.md` lists `friction_path` findings under **## Friction (advisory)**
only. Functional findings stay under **## Findings**.

## Tests

`tests/test_friction.py` covers score math, backtrack detection, emit gates,
summary sectioning, config load, a dedicated FakeDriver wizard that **does**
emit, and a short happy-path hunt that stays quiet.
