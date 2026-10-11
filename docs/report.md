# The campaign report

Every campaign writes one folder: `reports/<campaign-id>/`. SwarmQA writes
three views of the same results into it:

- `report.html`: one page to open in a browser and share.
- `summary.md`: the same content as plain markdown.
- The GitHub PR comment and check summary, when `[github]` reporting is on
  (see `docs/github.md`).

All three use the same order and the same words.

## The verdict

Each view starts with one line in plain words. Examples:

- `No problems found`
- `3 problems found, 1 critical, 2 medium`
- `1 problem found, 1 high; 2 more findings to check (advisory or not reproduced)`

A **problem** is a finding that is not advisory and was not shown to be
unreproducible. Two kinds of findings are "to check", not problems:

- **Advisory**: only a model or a soft heuristic reported it.
- **Not reproduced**: a confirmation re-run did not see it again.

The views show these in a separate, collapsed group, so they do not look
like failures.

## Open the page

The orchestrator writes `report.html` after `summary.md`. To write it again
for an old campaign:

```sh
aqa report --html
```

Open `reports/<campaign-id>/report.html` in any browser.

The page is one file. It has inline CSS and no scripts. It makes no network
requests and loads no fonts. Screenshots and videos are linked with paths
relative to the campaign folder. So you can zip the folder, or upload it as
a CI artifact, and the page still works.

The page follows the system light or dark mode. It works on a laptop and on
a phone.

## What the page shows

1. **Header.** The app (bundle id or app name), platform, version, campaign
   id, start time, duration and the verdict.
2. **Counts.** One row per severity, with the number of confirmed, not
   confirmed and advisory findings.
3. **Findings.** Problems first, then the findings to check. Each group is
   sorted by severity, then confirmed first, then by confidence. Each card
   shows:
   - the title, severity, category and confidence;
   - the confirmation result, for example `Confirmed 2/2`, `Flaky 1/2` or
     `Not reproduced 0/2`;
   - `advisory: model only` when only a model reported it;
   - the control it happened on, when known;
   - the steps, as a numbered list;
   - the screenshots, largest first (click one to open it full size);
   - the short video clip, or a link to the full video;
   - the suspected source files;
   - the replay command, `aqa replay <path>`;
   - "Also seen in N more places", with the list;
   - the details and the environment, in a collapsed block.
4. **Coverage.** Screens seen, controls tried out of the total, and a grid of
   screen thumbnails from `screen_graph.json` with visit counts.
5. **Shards.** One row per shard: name, kind, status, duration, stop reason
   and number of findings.
6. **Footer.** How to run again (`aqa run`), how to replay a finding, and the
   SwarmQA version.

## Inputs

The page reads these files from the campaign folder:

| File | Used for |
| --- | --- |
| `findings.json` | The findings. If it is missing or broken, the page uses the findings in `summary.json`. |
| `summary.json` | Shards, coverage counts and spend. If it is missing, the page reads `workers/*/result.json`. |
| `status.json` | The campaign id, when `summary.json` is missing. |
| `screen_graph.json` | The coverage section. |
| `media/`, `workers/<id>/` | Screenshots and videos. Paths in findings are relative to the campaign folder. |

Any file can be missing. The page still renders and says what is missing.

The app name, platform and version come from the findings' `environment`.
A campaign with no findings shows the campaign id instead.

## The PR comment

The comment uses the same order as the page, made short for GitHub:

1. The headline and the verdict line.
2. A table of problems: severity, title, confirmation, and count (the
   finding plus the other places it was seen).
3. One collapsed section per problem: steps, a screenshot (when
   `evidence_command` can upload it), the replay command, suspected sources,
   and the other places it was seen.
4. One collapsed block with the findings to check.
5. Links to the CI run, the campaign ids and the `report.html` path.

`github.max_findings` limits the number of findings in the comment. The rest
are counted in one line.

## For developers

- `swarmqa/report/html.py`: `write_html_report(campaign_dir)` writes the
  page and returns its path. It never raises; it returns None on error.
  Each section has its own small function.
- `swarmqa/report/summary.py`: `summary.md`, and the shared helpers
  `verdict`, `is_problem`, `confirmation_text`, `finding_sort_key` and
  `occurrence_count`.
- `swarmqa/github/report.py`: the PR comment and the check summary.
