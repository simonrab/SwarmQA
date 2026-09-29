# Evidence and findings v2

After the workers finish, `swarmqa.report.pipeline.finalize_findings` turns
the raw findings into the final evidence set:

```python
finalize_findings(campaign_dir, campaign_id, findings, *, config, repo=None, sha=None,
                  runner=None, state_root=None, full_run=True, ffmpeg=None) -> list[Finding]
```

Steps, in order:

1. **Dedup** within the run (`report/dedup.py`, `in_place=True`). The
   returned findings are the first input finding for each fingerprint, so a
   `CampaignResult` that already holds them shows the evidence.
2. **Repro** (`report/repro.py`): sets `repro` and writes
   `findings/<id>.repro.md`.
3. **Frames and clip** (`report/clips.py`).
4. **Source map** (`report/source_map.py`), when `repo` is given.
5. **Cross-run state** (`report/state.py`), in `state_root` or else `repo`.
   With neither, this step is skipped.
6. Rewrites `findings/<id>.md` (now with a Triage section) and writes
   `findings.json`.

Steps 2 to 4 are best effort. A failure is appended to
`environment["evidence_errors"]` and the rest still runs.

## findings.json

`reports/<campaign-id>/findings.json` is
`{"schema_version": 2, "campaign_id", "sha", "findings": [...]}` and
validates against `swarmqa/schemas/findings.v2.json`.
`write_findings_json` refuses to write a document that does not validate.
`load_findings_json` also reads v1 files and bare lists. `validate(payload)`
checks the JSON Schema subset the schema uses and returns error strings.

## Repro scripts

Each finding keeps its version-1 replay flow at `findings/<id>.replay.json`.
The agent loop and the scripted explorer already write it, and the pipeline
reuses it. A `launch` finding without one gets a one-step `launch` flow.
Other findings without one get no repro. `findings/<id>.repro.md` holds the
command:

```sh
aqa replay reports/<campaign-id>/findings/<id>.replay.json
```

The command should call:

```python
replay_finding(replay_path, config, *, driver=None, work_dir, finding=None,
               checks=None, driver_factory=None) -> ReplayResult
```

It runs the flow with `run_scripted` through a driver wrapper that runs the
checks around every tap, type, swipe and key. The checks default to
`default_checks()`, which are functional and layout. A leading `launch`
step is dropped because `run_scripted` launches the app itself. Steps keep
going after a failure, as they did in the recorded session.

If `finding` is not given, the finding is looked up by id in the
`findings.json` next to the replay's `findings/` directory. When it
reproduces:

| Finding kind | Reproduced when |
| --- | --- |
| `crash` | the final step crashes the app, or a crash with the same fingerprint is seen |
| `launch` | the app fails to launch |
| anything else | a check reports the same fingerprint at any step, or the final step fails |
| no finding found | any step fails |

`ReplayResult` has `reproduced`, `reason`, `finding_id`, `kind`,
`fingerprint`, `observed_fingerprints`, `final_step_failed`, `crashed`, and
the replay's `WorkerResult`.

## Clips and frames

`trim_clip` cuts `[t - 5s, t + 2s]` from the session video into
`media/<id>.clip.mp4` and sets `evidence.video_clip`. It tries
`ffmpeg -ss S -i VIDEO -t D -c copy OUT` first, then falls back to a
libx264 re-encode. `t` is the failure's offset into the recording, taken
from the finding's environment:

- `event_ts`: when the failure happened, in Unix epoch seconds
- `video_start_ts`: when recording started, in Unix epoch seconds
- `video_offset_s`: the offset itself. It wins over the pair above.

ffmpeg is optional. The clip is skipped silently when ffmpeg is missing,
when the timestamps or video are missing, or when both commands fail.
`collect_frames` merges `screenshots` into `evidence.frames` and orders them
by file modification time when every file exists.

## Source map

`map_finding` runs `git -C <repo> grep -n -I -F --full-name -e '"<needle>"'`.
It searches for the accessibility identifier first, then for the exact
label, each quoted as a string literal. Hits in `.swift`, `.m`,
`.storyboard`, `.xib` and `.strings` files rank first. At most 5 `path:line`
entries go to `suspected_sources`. A directory that is not a git repo, or a
search with no matches, gives no entries.

The identifier and label come from `environment["target_identifier"]` and
`environment["target_label"]`. When neither is set, they come from the last
targeted step of the replay flow.

## Cross-run state

`<root>/.aqa/state/findings.json` maps each fingerprint to `first_seen`,
`last_seen`, `campaigns`, `count`, `status` (`open` or `fixed`), `title` and
`kind`. `mark_seen(findings, campaign_id, *, root, full_run=True)` returns
`new`, `recurring`, `regressed`, and `fixed` fingerprints. It also sets
these keys in each finding's environment:

- `seen_before`: `"true"` or `"false"`
- `first_seen`
- `regressed`: `"true"` when a fixed fingerprint came back

A full run marks every open fingerprint it did not see as `fixed`. Pass
`full_run=False` for a stopped or filtered campaign. Marking the same
campaign twice does not count it twice.

## Environment keys the producers set

| Key | Set by | Used for |
| --- | --- | --- |
| `event_ts` | agent loop / scripted explorer | clip |
| `video_start_ts` | agent loop / scripted explorer | clip |
| `video_offset_s` | optional, instead of the pair | clip |
| `target_identifier` | agent loop / scripted explorer | source map |
| `target_label` | agent loop / scripted explorer | source map |
| `seen_before`, `first_seen`, `regressed` | state store | summary, triage |
| `evidence_errors` | pipeline | debugging |

## summary.md

Each finding bullet is followed by its category, confidence, and
seen-before or regressed status. Then come its suspected sources, its clip,
and its repro command. Firm findings are under `## Findings`. Advisory
findings (model-only or soft heuristics) come next under
`## Advisory findings`. Friction findings stay under
`## Friction (advisory)`.
