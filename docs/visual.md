# Visual diff

Workers compare a fresh screenshot to a stored baseline and emit a score plus a reviewable diff image. Baselines change only through `update_baselines`. A compare never writes the baseline file, so concurrent workers can read the same set.

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
