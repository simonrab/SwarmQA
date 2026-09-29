# Visual diff

Workers compare a fresh screenshot to a stored baseline and emit a score plus a reviewable diff image. Baselines change only through `update_baselines`. A compare never writes the baseline file, so concurrent workers can read the same set.

Pixel diff only flags pixels that changed. It does not decide whether a screen looks wrong, and it does not write prose. Appearance judgment is a separate step, below.

## Compare

```python
compare_screenshot(current, baseline, diff_out, *, threshold) -> DiffResult
```

`DiffResult` fields:

| Field | Meaning |
| --- | --- |
| `name` | Baseline filename stem (`baselines/home.png` → `home`) |
| `passed` | `score <= threshold` |
| `score` | Fraction of pixels that changed, from 0 to 1 |
| `diff_path` | PNG that was written, or `None` when nothing was written |
| `message` | Empty on a clean pass; a short reason otherwise |

Both images are loaded with Pillow and converted to RGBA. When the sizes differ, the current image is scaled to the baseline size with Lanczos resampling. A pixel counts as changed when any channel's absolute delta is greater than 16 (on the 0–255 scale). Alpha is included. `score` is `changed_pixels / total_pixels` after that scale. An empty image scores 0.

`passed` is true when `score <= threshold`. The campaign default lives on `VisualConfig.threshold` (`0.01`); callers pass it in.

The diff PNG matches the baseline size:

- Unchanged pixels keep the current image's RGB values.
- Changed pixels are painted bright red (`255`, with a dim trace of the current green and blue) so the mismatch is visible in review.

`diff_out`'s parent directory is created when the diff is written. If `diff_out` resolves to the baseline path, the compare still returns a score and does not write. That guard is why a compare cannot replace a baseline, even when the caller points the diff at the baseline file.

A missing baseline file returns `passed=False`, `score=1.0`, `diff_path=None`, and a message that contains `baseline is missing`. The baseline path is not created.

A missing current screenshot returns `passed=False` with `current screenshot is missing`. Corrupt images raise the Pillow error.

## Update baselines

```python
update_baselines(source_dir, baseline_dir) -> list[Path]
```

This is the only baseline write path. It creates `baseline_dir` (including parents) and copies each top-level `*.png` file from `source_dir` into that directory, overwriting a same-named baseline. Nested PNGs and other suffixes are left alone. The return value is the destination paths, sorted by filename. A missing source directory raises `FileNotFoundError` and does not create `baseline_dir`.

The CLI already calls this:

```bash
aqa baseline update --from-dir reports/<id>/media --baseline-dir baselines
```

## Findings

When a caller wraps a failed `DiffResult` in a `Finding`, set `kind="visual"`. Put the diff path in the finding evidence. Functional findings stay on their own kinds (`assertion`, `crash`, and the rest). The orchestrator owns that wrap; this module only returns `DiffResult`.

Appearance judgments use `kind="visual_judgment"` so a report can list both. The campaign summary keeps pixel diffs and judgments together under **## Findings**, and tags each line with its kind.

## Judgment

`visual.judgment` looks at a screenshot that the campaign already saved and returns either `fine` or a short written judgment. It does not need a baseline. It runs only when `visual.judgment.enabled` is true (the default is false).

Call sites:

- after a scripted `screenshot` action
- after an exploratory hunt saves a screenshot
- after the visual shard captures the current screenshot, including when the baseline file is missing

A fine screen adds nothing. A bad screen writes one `Finding`:

| Field | Value |
| --- | --- |
| `kind` | `visual_judgment` |
| `severity` | `medium` |
| `title` | the judgment, truncated to 120 characters |
| `details` | the full judgment |

`provider = "command"` (the default) runs one command. The command is the value of the environment variable named by `command_env` (default `AQA_VISUAL_JUDGE_COMMAND`). When that variable is unset, `command` in the config is used. Claude Code or Codex can be that command. The PNG path is the last argument. Stdout must be one JSON object:

```json
{"ok": true}
```

```json
{"ok": false, "judgment": "The save button overlaps the title."}
```

A non-zero exit, a timeout, or unparseable output fails open: no finding, and the campaign keeps going. The worker result `error` field gets a one-line note only when that field was empty. The command is not retried.

`provider = "fake"` does not spawn a process. It uses the canned `judgment` string from config. An empty string, or the text `fine`, means the screen is fine. Anything else is the written judgment. Tests use this provider.
