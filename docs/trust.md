# Findings you can trust

One false finding in a pull request teaches a team to ignore the tool.
So after a run, SwarmQA does two things before it writes `findings.json`:

1. It groups repeats. The same problem in many places becomes one finding.
2. It re-runs each finding. It replays the finding on a fresh launch and
   records whether the problem showed again.

Grouping runs first, so there are fewer replays. The code is in
`swarmqa/confirm/`.

## Settings

Put these in the `[findings]` table. The values below are the defaults.

```toml
[findings]
confirm = true              # re-run findings before they are reported
confirm_runs = 1            # replays per finding, each on a fresh launch
confirm_kinds = ["crash", "unresponsive", "timeout", "error_state",
                 "assertion", "missing_control", "launch"]
max_confirm = 20            # most findings to re-run, most severe first
confirm_budget_s = 300      # total time for all re-runs, in seconds
skip_advisory = true        # do not re-run model-only findings
drop_not_reproduced = false # remove findings that never showed again
group = true                # fold the same problem in many places into one
group_min = 2               # fold only when at least this many match
```

An unknown key or a bad value stops the run with a config error. The error
lists every problem.

## Re-runs

SwarmQA replays the finding's flow (`findings/<id>.replay.json`) with the
same rule as `aqa replay` and `aqa verify` (see [evidence.md](evidence.md)).
Each replay gets a new driver, so the app starts fresh. Its files go to
`<campaign>/confirm/<finding-id>-<n>/`.

The result goes in three fields:

| `confirmation`   | Meaning                                   | What changes                          |
|------------------|-------------------------------------------|---------------------------------------|
| `confirmed`      | Every replay showed the problem.          | Nothing.                              |
| `flaky`          | Some replays showed it.                   | `confidence` drops to 0.6 at most.    |
| `not_reproduced` | No replay showed it.                      | `advisory = true`, `confidence` 0.3 at most. |
| `skipped`        | No replay flow, or no replay could run.   | Nothing.                              |
| none (`null`)    | Not re-run (settings, limit or budget).   | Nothing.                              |

`confirm_attempts` counts the replays that ran. `confirm_hits` counts the
replays that showed the problem. A replay that could not run (the app did
not launch, or the driver could not start) does not count. A short note
goes at the end of `details`.

A `not_reproduced` finding is advisory. It no longer fails a GitHub check
(unless `github.fail_on_advisory` is set). When a failed shard has only
findings that did not reproduce, the shard is marked `passed`, so the run
exit code does not fail on it. With `drop_not_reproduced = true` the
finding is removed from the report.

### Which findings are re-run

- Only kinds in `confirm_kinds`. The default kinds are the ones where a
  replay can show the problem again: crashes, hangs, dead taps, endless
  spinners, error screens, failed asserts, missing controls and launch
  failures.
- Layout findings (`visual`) are not in the default list. A layout check
  sees the same screen tree each time, so a replay seldom changes the
  answer. Add `"visual"` to re-run them anyway.
- Model-only findings (`advisory`) are skipped when `skip_advisory = true`.
- Findings go most severe first. At most `max_confirm` are re-run.
- The budget is checked before each replay. When `confirm_budget_s` runs
  out, SwarmQA stops. The rest keep `confirmation = null`. A replay that
  already started finishes, so the total can go a little over.
- A launch failure without a replay flow gets a one-step `launch` flow.

### Devices

SwarmQA closes every replay driver, also after an error. With `[swarm]`, a
re-run leases a device and closing the driver returns it. If
`swarm.keep_devices = false`, the pool is closed before re-runs start, so
SwarmQA skips the re-runs. With a non-local backend (`vm`, `cloud`) the
re-runs are skipped too. A test that injects its own driver factory gets
that factory, without a lease.

## Grouping

Two findings are the same problem when all of these match:

- `kind`, `category` and `advisory`;
- the title, after SwarmQA takes out the element names: quoted names
  (`button "Share"`), screen points (`at (120, 40)`), and for layout
  findings without quotes, the text after the first colon
  (`Low text contrast: Due today`).

So `Small tap target: button "Share"` and `Small tap target: button "Edit"`
match. `Small tap target: switch "Wi-Fi"` does not, because the role is
different.

Crashes and launch failures are never grouped. A crash in another place is
usually another bug.

When at least `group_min` findings match, SwarmQA keeps one:

- It keeps the most severe one, then the most confident one.
- Its `severity` becomes the highest in the group.
- The other element names go to `occurrences`. The count is
  `1 + len(occurrences)`.
- Worker ids are merged. Screenshots are merged, up to 5.
- The title reads `Small tap target: button "Share" (+3 more)`.
- `fingerprint` and the replay stay those of the kept finding.

The other findings are removed from the report and from each worker
result.

## Errors

Trust work never fails a campaign. If something inside it breaks, SwarmQA
prints a warning and reports the findings as they were.
