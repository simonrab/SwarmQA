# Checks

Package: `swarmqa/checks/`. Ownership: **WP-B3** in `docs/OWNERSHIP.md`.

A check turns one `StepContext` (see `swarmqa/checks/protocol.py`) into a list of `CheckIssue`s. The agent loop runs every check after every action. It runs checks with `per_screen = True` once per new screen fingerprint. It turns issues into findings with `CheckIssue.to_finding`; checks never build a `Finding` themselves. A check never raises for an app problem. Driver or model trouble fails open: the check returns no issues and puts a one-line note in `last_error` for the caller to log.

| Check | Module | `per_screen` | Settings |
| --- | --- | --- | --- |
| `FunctionalCheck` | `functional.py` | no | `FunctionalSettings` |
| `LayoutCheck` | `layout.py` | yes | `LayoutSettings` |
| `BaselineCheck` | `baseline.py` | yes | `BaselineSettings` |
| `JudgeCheck` | `judge.py` | yes | `JudgeSettings` + a `ModelProvider` |
| `CommandJudgeCheck` | `judge.py` | yes | `VisualJudgmentConfig` (the `command` / `fake` backend in `visual/judge.py`) |
| `FrictionCheck` | `friction.py` | no | `FrictionConfig`; call `finish()` at session end |

`default_checks(settings: ChecksSettings | None = None, provider=None)` builds functional and layout checks, plus a baseline check when `settings.baseline` is set. It adds the model judge when a provider is given. Without a provider, it adds the command judge when `settings.command_judge.enabled` is true. The settings are local dataclasses. The lead maps them onto `CampaignConfig` later.

**Advisory issues.** Anything that only a model or a heuristic reported is `advisory=True` and has a confidence below 1. The advisory issues are: judge issues, console errors, truncation, contrast, small tap targets, overlaps that involve text, slow (timing-only) hangs, missing baselines, and friction.

**After a crash.** The agent loop passes `crashed=True` with an empty `after.tree` and no screenshot. The functional check reports the crash and stops there. Layout and friction return nothing for an empty screen. Baseline and the judges need a screenshot, so they skip too.

## Functional (`functional.py`)

| Rule | Fires when | Issue |
| --- | --- | --- |
| Crash | `ctx.crashed`, or `driver.crash_reports_since(since_ts)` returns reports | `crash` / `crash`, critical. Title `Crash on <target>` (target = identifier, else label, else role, else `(x, y)` for `tap_point`), the same title as the agent loop's own crash issue so both give one fingerprint. Without a target the title is `App crashed[: <summary>]`, with numbers and addresses shown as `N`. Details list each report's process, summary and path. `extra["crash_report"]` holds the first path. The screenshot falls back to `before.screenshot`. A crash suppresses every other functional rule for that step. When the step did not crash and the app answered, reports whose own crash time (`extra["capture_time"]`, the `.ips` `captureTime`) is more than 2 s before `since_ts` are dropped: ReportCrash can write an `.ips` tens of seconds late, and the driver filters by file time, so an earlier session's crash would otherwise land on the next shard's first step. |
| Hang (error) | `ctx.error` matches timeout text (`UITimeoutError`, `timed out`, `timeout`, `not responding`, `unresponsive`, `hang`/`hung` as whole words) | `unresponsive` / `broken`, high |
| Hang (timing) | `after.ts - since_ts > hang_after_s` (default 15 s; 0 turns it off) | `unresponsive` / `broken`, medium, advisory, confidence 0.6 |
| Dead tap | See below | `unresponsive` / `broken`, medium, confidence 0.8, with `element` and `bbox` |
| Error alert | An `alert` / `sheet` / `dialog` / `popover` / `actionsheet` whose text (its label, value and all descendant labels and values) matches `error(s)`, `failed`, `failure`, `went wrong`, `couldn't`, `could not`, `unable to`, or `try again later`, and that was not already on the previous screen | `error_state` / `broken`, high |
| Endless spinner | An `activityindicator` / `progressindicator` (outermost one, on the page on screen, not a stopped spinner with value `0`) is still there `spinner_timeout_s` (10 s) after it first appeared. With `spinner_wait` (default) the check polls `driver.observe(screenshot=False)` every `spinner_poll_s` (1 s) until the spinner goes or time is up, so the verdict does not depend on the explorer lingering. Each spinner (identifier, else label) is reported once per session. | `timeout` / `broken`, medium, confidence 0.8, with `element` and `bbox` |
| Console errors | `driver.logs_since(since_ts)` has `error` or `fault` lines, after dropping `SYSTEM_LOG_NOISE` (UIKit gesture-gate timeouts, XCTest `animationDidStop` warnings, `UIKeyboardLayoutStar`, automation type mismatches, `fopen failed for data file`, off-window snapshots, CA event failures, `CHHapticPattern` file reads), `console_ignore`, and, with `console_app_only` (default), lines from system subsystems (`console_system_subsystems`, default `com.apple.`) unless the subsystem is the app's bundle id (`driver.bundle_id`). Lines with an empty subsystem (the app's `print`/`NSLog`) are kept. | One `error_state` issue per step, advisory, confidence 0.5. Severity is low, or medium if any line is a fault. The title uses the first message with numbers shown as `N`. Details hold up to `max_console_lines` lines. |

**Dead tap.** A dead tap is reported only when all of these hold:

- The action is `tap` (resolved against `before.tree` with the driver's query rules) or `tap_point` (the smallest interactive element under the point).
- The step has no error and no crash, and it is not the first screen.
- The element is enabled and interactive. False-positive guards:
  - Text fields and other fields are skipped, because they take focus without changing the tree.
  - Sliders and steppers are skipped.
  - Static text, images and containers are skipped. A `tap_point` whose smallest hit is text, with no interactive element under the point, is skipped too.
  - Elements with an interactive descendant are skipped: XCUITest taps the centre of a labelled SwiftUI Toggle row, which on iOS misses the switch itself.
  - Elements that reach into the keyboard, or the 60 pt above it (the suggestion bar), while a keyboard is up are skipped: the tap may have landed on the keyboard.
- The visible signature (`_tree.visible_signature`) is identical before and after. It covers the role, label, identifier, value, enabled flag and frame (rounded to whole points) of every element on the page on screen. So a toggle whose value changed, a new element, or any movement means the tap did something. It is order- and nesting-free and leaves out stale-page nodes (see Layout), anonymous `other` containers, and scroll indicators (`Vertical scroll bar, 1 page` and their children): UIKit flashes those on any touch inside a scroll view, and the explorer often observes the previous screen mid-transition, so without this every dead button on a scrolling or freshly pushed screen was hidden.
- No new modal appeared.
- When both screenshots exist and neither observation caught a navigation transition, at most `pixel_change_tolerance` (0.5%) of pixels changed, not counting the tapped control's own frame (plus 4 pt), whose press highlight is still fading when the screen is read. Unreadable or differently sized screenshots count as changed.

## Layout (`layout.py`)

Frames are points. Elements with a zero or negative width or height are treated as hidden and skipped. Every layout issue is kind `visual` and carries `element`, `bbox` and the screenshot.

**Only the page on screen.** Right after a NavigationStack push or pop, the iOS snapshot holds both pages: the leaving page sits in a container exactly the size of the screen but shifted off the origin (parallaxed to about x = -30% of the width), and the snapshot may even nest the arriving page inside it. `_tree.stale_nodes` gives each node the page of its nearest screen-sized ancestor (or itself) and drops nodes whose page is off the origin. Every layout rule ignores stale nodes. When any node is stale the screenshot is mid-slide, so contrast is skipped for that observation (the settled screen is checked when it is observed).

| Rule | Heuristic and thresholds | Guards | Issue |
| --- | --- | --- | --- |
| Truncation (ellipsis) | A text element whose label ends in `…` or `...`, with at least 8 characters and 2 words before the ellipsis | Progress text is skipped (`Loading…`, `Saving your changes...`, where the first or last word is a progress word such as loading, saving, searching or more). Buttons are skipped, so macOS `Export…` is fine. | `visual`, low, advisory, 0.6 |
| Truncation (size) | The frame cannot hold the label even at `min_font_pt` (11 pt) with glyphs `narrow_char_em` (0.4 em) wide, on as many 1.2 × font lines as the frame height allows. This under-estimates the space real text needs, so only clear overflows fire. | Labels under 4 characters | same |
| Off screen | An interactive element that is partly outside `obs.size`, by more than `offscreen_tolerance` (2 pt) | Fully off-screen elements (pages, hidden views), elements inside a scroll container (`scrollarea`, `table`, `list`, `collectionview`, `webview`, …), and elements whose window frame itself hangs off the display | `visual`, medium, 0.8 |
| Overlap | Two interactive elements, or labelled text elements, on screen in the same layer whose intersection is at least `overlap_ratio` (25%) of the smaller frame and at least 2 × 2 pt. When one side is text whose frame reaches down into an element that starts at least `overflow_min_offset` (20 pt) below the text's top and is shorter than the text, it is reported once per text element as **text runs into the elements below it** (text taller than the box it is drawn in: clipped or drawn under the next element) instead of as pairwise overlaps. | Stale-page nodes; ancestor/descendant pairs; geometric containment (a label inside its button, a badge inside an icon); pairs in different windows or modals; pairs where only one side is inside a scroll container (content scrolled under fixed chrome); at most `max_elements` (300) candidates | `visual`, medium and non-advisory when both are interactive; low and advisory when text is involved. Text overflow: `visual`, medium, advisory, 0.7, on the text element. |
| Tap target | An enabled `button`, `imagebutton`, `link`, `tab` or `popupbutton` smaller than `min_target_ios` (44 pt) or `min_target_macos` (24 pt) on either side, chosen by `platform`, with 0.5 pt of slack | Disabled controls; inline links inside text; switches, checkboxes, sliders, fields and segmented controls, which have system-defined sizes; buttons inside a navigation bar, toolbar or tab bar, or inside a segmented control, picker or menu (UIKit pads their hit area beyond the reported frame); buttons inside a list or menu cell within 4 pt of the minimum height (the row is the target; iPhone pull-down menu rows are 42 pt) | `visual`, low, advisory, 0.7. The accessibility frame can be smaller than the real hit area. |
| Missing label | An interactive element (buttons, toggles, fields, sliders, tabs, …; not cells) with no label. For buttons, image buttons, links, tabs and pop-up buttons an identifier does not count, because VoiceOver does not read it; other roles with an identifier are skipped (their name is usually a separate label element) | Fields with a value or placeholder; elements with a labelled descendant (an SF Symbol image gives its own label); the inner part of a labelled control (the switch inside a labelled toggle row); stale-page and zero-size elements | `visual` with category **`broken`**, medium. VoiceOver users and automation cannot use an unnamed control, which is a functional failure rather than a cosmetic one. The title names the identifier when there is one, else the frame centre rounded to 10 pt, so different unlabeled controls do not collapse into one finding. |
| Contrast | For each enabled, labelled text element fully on screen and not inside a system alert, action sheet or popover (drawn on translucent material over a dimmed screen), crop its frame from the screenshot (points × `obs.scale`) and estimate the WCAG 2 contrast ratio. Flag under `contrast_min` (default 3:1), or under `contrast_large_min` (3:1) when the frame is at least `large_text_height` (23 pt) tall, less `contrast_margin` (0.2). The default is WCAG's large-text / UI-component level rather than 4.5:1 because iOS's own secondary label colour measures 3.3-4:1 on real screenshots (and a bordered destructive button about 2.9:1); set `contrast_min = 4.5` and `contrast_margin = 0` for strict WCAG AA. | See sampling below | `visual`, medium, advisory, 0.6. `extra["contrast"]` holds the ratio. |

**Contrast sampling (`sample_contrast`).** Everything runs in Pillow C code, with no per-pixel Python loop:

- The crop is downsampled with nearest-neighbour when it is over 40k pixels.
- Colours are posterised to 5 bits per channel, and each bucket is read at its midpoint.
- The most common colour is the background. It must cover at least 40% of the crop.
- Crops with more than 256 colours (photos, gradients) are skipped.
- The foreground is the colour with the highest contrast against the background, among colours covering at least `contrast_min_foreground` (0.4%) of the crop. So anti-aliased edges do not lower the estimate, and thin footnote text in a large padded frame still counts.
- Both colours are then re-read as the mean of the real pixels in their buckets, so the ratio is not off by the bucket width.
- Uniform crops, crops outside the image, and crops smaller than 3 px are skipped.

## Baseline (`baseline.py`)

- **Where baselines live.** The baseline is `baseline_dir/<key>.png`. The key is the name passed to `BaselineCheck(key=...)`, else `ctx.screen_id`, made file-safe.
- **What counts as changed.** A pixel is changed when any RGBA channel moves by more than 16 (`channel_delta`). The diff uses `ImageChops.difference`, a threshold `point` table, `ImageChops.lighter`, and a `histogram` count. The same `diff_images` and `highlight` now back `visual/diff.py`, whose C6 behaviour, including resizing, is unchanged.
- **Pass or fail.** A screen passes when the changed fraction is at most `threshold` (0–1, default 0.01). Otherwise it is a `visual` / `visual` medium issue with `extra["score"]`. When `diff_dir` is set, the check writes `<key>-diff.png` there with changed pixels in red.
- **Size mismatch.** A size mismatch is its own issue (`Screenshot size differs from baseline`). The check never resizes.
- **Missing baseline.** The behaviour follows `on_missing`:
  - `report` (the default, matching C6's "baseline is missing") gives a low, advisory issue.
  - `record` also copies the screenshot to `record_dir`, for `aqa baseline update --from`.
  - `ignore` gives nothing.
- **Write paths.** The check never writes into `baseline_dir`; `update_baselines` stays the only write path.

## Judges (`judge.py`)

`JudgeCheck(provider, JudgeSettings())` calls `provider.judge_screen(obs, Rubric(name))` for each rubric in `rubrics` (default `visual`, then `confusion`):

- **No screenshot.** Without a screenshot it makes no call.
- **Issues.** Each `JudgedIssue` becomes a `visual_judgment` issue with the model's category, severity, element and bbox. It is `advisory=True` with the model's confidence, capped at 0.99.
- **Confidence floor.** Issues below `rubric.min_confidence` are dropped. `JudgeSettings.min_confidence` overrides the floor.
- **Errors.** A `ModelError` on one rubric skips that rubric and is recorded in `last_error`.
- **Spend.** Each call's `Usage` is appended to `usages` for the spend meter.

`CommandJudgeCheck(VisualJudgmentConfig)` wraps `visual.judge.judge_screenshot`, which is the `command` fallback, or `fake`. `fine` gives no issues. Any other judgment becomes one advisory `visual_judgment` issue with confidence 0.5. `JudgeError` fails open.

## Friction (`friction.py`)

Friction depends on the whole path compared with a gold path, so it is decided once per session, not per step:

- `FrictionCheck.run` feeds each step into a `FrictionSession`. Taps map to `click` and failed actions count as dead ends. `run` also records the step's KLM operator time and returns no issues.
- `done` and `give_up` are not user acts. They only set `goal_reached`.
- The agent loop calls `finish()` at session end. It returns at most one advisory `friction_path` / `confusing` issue, through the same gates as `friction/emit.py`.
- The title (`High-friction path (<persona>): <intent>`) has no numbers, so score drift does not fork the finding.
- `FrictionCheck.for_shard(shard, config)` takes the gold path from the shard's actions or its `gold_steps:N` tag, and takes the context from `shard.id`, `shard.goal` and `shard.tags`.

**KLM fix.** `metrics_for` used to set `klm_ratio = step_ratio`. That scored the same extra steps twice: 35 points from the step term plus 10 from the KLM term. For example, a 3× path with no other pathology scored 45 instead of 35. `klm_ratio` is now an optional input: the mean KLM operator time per observed step divided by the gold mean. Without an estimate, or with `klm = false`, the term is neutral (1.0). The operator times are M 1.35 s, P 1.1 s, B 0.2 s, K 0.28 s per keystroke, and D 0.3 s per drag:

- tap, click, back or menu: M+P+B
- type: M+P+B+K per character
- swipe or scroll: M+P+B+D
- key: M+K per key

`klm_ratio_for` and `gold_klm_from_actions` compute the ratio.

## Shard tags from ingest (`intent/ingest.py`)

`build_queue` now fills `Shard.tags` in this order, without duplicates:

1. **Front matter.** In a leading `---` … `---` block of a markdown intent, only the `tags:` key is read, as `tags: [a, b]`, `tags: a, b`, or `tags:` followed by `- a` lines. Other keys are ignored. The block is kept out of the goal.
2. **`## Tags` section.** Bullets or comma-separated lines under this heading. The section is kept out of the goal.
3. **`flow:<slug>`.** Added to every markdown and JSON intent shard, using `slug` of the shard name.
4. **`gold_steps:N`.** Added to the exploratory companion of a scripted markdown intent, where N is the number of scripted steps, unless a `gold_steps:` or `gold:` tag is already declared. `scripted_steps_from_shard` then gives the explorer's friction meter a scripted gold path instead of a synthesized one.

Tags are trimmed and lower-cased, and inner whitespace becomes `-`. The existing consumers read them: `allow_step_ratio:N`, `gold_steps:N`, and the wizard markers (`wizard`, `form`, `onboarding`). JSON flows cannot declare tags, because the flow schema rejects unknown fields.
