# PlantedBugs

PlantedBugs is the benchmark app for SwarmQA. It is a small SwiftUI task list for iOS and macOS, built from one multiplatform target. Bundle id: `dev.swarmqa.PlantedBugs`.

**The bugs in this app are intentional.** Each one is listed in `bugs.json`, and the planted-bug benchmark (`aqa bench fixtures/PlantedBugs`) scores the swarm's findings against that list. Do not fix them unless you are doing so on purpose (for example, to test `verify_fix`), and restore them afterwards. Everything not listed in `bugs.json` is meant to work, because the benchmark's false-positive count depends on it. If you find an unlisted bug, fix it or add it to `bugs.json`.

## The app

- **Home** (`Tasks`): a list of five seeded tasks (`home.row.<id>`), a "done" summary, a Weekly stats link, a tip card, and a toolbar with filter, add and settings buttons.
- **Add Task** sheet: title, priority, Cancel and Save. Save stays disabled until the title has text.
- **Task detail**: title, priority badge, due date, notes, a Completed toggle, an Attachments link, and Share and Delete buttons. Delete asks for confirmation.
- **Attachments** and **attachment preview**. Only "Finish quarterly report" has attachments.
- **Weekly stats**.
- **Settings**: display name, reminders, a read-only sync status, then Advanced > Data & Storage > Cloud, and About.

State lives in memory only, so every launch starts from the same seed data.

## Planted bugs

| id | identifier | category | where |
| --- | --- | --- | --- |
| `dead-button` | `detail.shareButton` | broken | Task detail |
| `clipped-text` | `detail.notes` | visual | Task detail ("Finish quarterly report") |
| `overlapping-views` | `detail.priorityBadge` | visual | Task detail |
| `crash-attachment` | `attachments.row.scan` | crash | Report task > Attachments > expenses-scan.pdf |
| `endless-spinner` | `stats.loadingSpinner` | broken | Weekly stats |
| `confusing-sync-settings` | `settings.sync.pauseToggle` | confusing | Settings > Advanced > Data & Storage > Cloud |
| `missing-label` | `home.filterButton` | confusing | Home toolbar |
| `low-contrast` | `home.tipLabel` | visual | Home |

`bugs.json` has the full description and path for each. In the Swift sources, each bug is marked with a `PLANTED BUG (<id>)` comment.

## Building

Xcode 26 or later. No team or provisioning is needed: the iOS build targets the Simulator, and the macOS build is signed to run locally (`CODE_SIGN_IDENTITY=-`).

```sh
./build.sh          # both platforms
./build.sh ios      # iOS Simulator only
./build.sh macos    # macOS only
```

The outputs are `build/ios/PlantedBugs.app` and `build/macos/PlantedBugs.app`. The script prints them last, as `ios=<path>` and `macos=<path>`. Build logs go to `build/<platform>-build.log`. DerivedData goes outside the checkout, to `~/Library/Developer/Xcode/DerivedData/PlantedBugs-<hash of checkout path>` (set `PLANTEDBUGS_DERIVED_DATA` to change it). Building in place fails when the checkout is in an iCloud-synced folder such as `~/Documents`, because codesign rejects the Finder and file-provider attributes that iCloud adds.

To run the app on a booted simulator:

```sh
xcrun simctl install booted build/ios/PlantedBugs.app
xcrun simctl launch booted dev.swarmqa.PlantedBugs
```

The Xcode project is hand-written. It uses a synchronized folder (`PBXFileSystemSynchronizedRootGroup`), so new `.swift` files under `PlantedBugs/` are picked up without editing `project.pbxproj`.

## Accessibility identifiers

Every interactive element, and every element a bug is attached to, has a stable dotted `accessibilityIdentifier`: `<screen>.<element>`, for example `home.addButton`, `detail.shareButton` and `settings.sync.applyButton`. Rows use `<screen>.row.<model id>`, for example `home.row.report` or `attachments.row.scan`. The seeded task ids are `report`, `groceries`, `dentist`, `plants` and `taxes`. Tasks added at runtime get `new-1`, `new-2`, and so on.

## bugs.json schema (version 1)

```json
{
  "schema_version": 1,
  "app": "PlantedBugs",
  "bundle_id": "dev.swarmqa.PlantedBugs",
  "bugs": [
    {
      "id": "dead-button",
      "identifier": "detail.shareButton",
      "screen": "Task detail",
      "path": ["home.row.report"],
      "category": "broken",
      "kind": "unresponsive",
      "platforms": ["ios", "macos"],
      "description": "..."
    }
  ]
}
```

| field | meaning |
| --- | --- |
| `id` | Stable kebab-case name for the bug. |
| `identifier` | Accessibility identifier of the element the bug is on. A finding that names this element, or has a screenshot of it, should match. |
| `screen` | Human-readable screen name. |
| `path` | Accessibility identifiers to activate in order, starting from the Home screen after launch, to reach the bug. It is empty when the bug is on Home. For flow bugs such as `confusing-sync-settings`, the path is the whole flow. |
| `category` | `broken`, `visual`, `confusing` or `crash`, as in Findings v2 (`swarmqa.models.Finding.category`). |
| `kind` | A `Finding.kind` whose default category (`swarmqa.models.category_for_kind`) is `category`. |
| `platforms` | The platforms the bug appears on: `ios`, `macos` or both. |
| `description` | What is wrong, in a sentence or two. |

A scorer should match a finding to a bug by `category` together with the element (`identifier`) or screen. It should not require an exact `kind`. For example, a model may report the missing label as `visual` rather than `confusing`, and a scorer may accept either.

## Checking bugs.json

```sh
python3 check_bugs.py
```

The script uses only the standard library. It validates every field, checks that each `kind` maps to its `category`, and checks that every identifier (the bug's own and each step in `path`) appears in an `accessibilityIdentifier(...)` call in `PlantedBugs/*.swift`. Run it after changing either file.
