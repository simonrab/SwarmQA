# macOS app driver

`MacOSDriver` (`swarmqa.driver.macos`) is the session driver for a macOS `.app`. `IOSSimulatorDriver` (`swarmqa.driver.ios`) is the session driver for an iOS Simulator `.app`. Both satisfy `AppDriver`: launch, relaunch, accessibility tree, click, type, keychord, scroll, menu select, wait, screenshot, video, and build metadata.

Set `app.platform` to `ios` (or pass `kind="ios"` to `create_driver`) to select the Simulator. The default platform is `macos`.

The module imports on Linux. Campaigns and unit tests that are not on a Mac should use `FakeDriver` (`kind="fake"`, the default off darwin). Calling `launch` when `sys.platform` is not `darwin` raises `BackendUnavailable` and tells the caller to use the fake driver or a Mac host.

## Accessibility permission

UI actions go through **System Events** via `osascript`. macOS only allows that after the process that runs `osascript` is on the Accessibility list.

Grant permission before a real session:

1. Open **System Settings → Privacy & Security → Accessibility**.
2. Enable the app that launches the worker. That is Terminal, iTerm, Cursor, or the Python host, whichever process is the parent of `osascript`.
3. If System Events itself was denied, remove it from the list and let macOS prompt again on the next `osascript` call.
4. Quit and reopen the host after changing the toggle. macOS keeps the old decision for the life of the process.

A denied session surfaces as `BackendUnavailable` and names Accessibility permission. Screen recording permission is separate: `screencapture` and `ffmpeg` avfoundation capture need **Screen Recording** for the same host process.

The driver does not embed a private Accessibility framework. When a future host can import the Accessibility APIs, System Events remains the supported actuation path.

## Session isolation

One `MacOSDriver` instance is one app session for one worker.

- The driver does **not** take an exclusive lock on the host display, menu bar, or Accessibility service.
- Launch starts a new instance (`open -n`, or the bundle executable under `Contents/MacOS` when that file is present) and passes `target.launch_args` and `target.env`.
- `close` and `relaunch` quit only that session. `relaunch` quits and launches again. `close` is safe to call more than once.
- Parallel GUI sessions on one Mac desktop are unreliable because they share one window server. The orchestrator places this driver in a VM or cloud Mac when true GUI parallelism is required. The local backend is for a small worker count, not for a fleet of Accessibility sessions on one display.

## Launch, tree, and actions

- `target.path` is the `.app` bundle. A missing bundle raises `AppMissingError`. A path that points at a binary inside the bundle is resolved back to the `.app`.
- When `path` is empty, the driver runs `target.build_command` with `/bin/sh -c` and reads a `.app` path from the command output. If the command fails or prints no bundle, launch raises `AppMissingError`.
- After `open` or the executable starts, the driver checks that the process is still alive. An exit during launch or during a later action raises `AppCrashedError`.
- `accessibility_tree` reads windows, their contents, and menu bars into `UIElement` nodes (role, label, identifier, value, enabled, frame, children).
- Element matching uses `swarmqa.driver.query`: role equality is case-insensitive, label match is a case-insensitive substring, and identifier and value are exact. `click`, `type_text`, `scroll` (when a target is passed), and `wait_for` all use that lookup. `wait_for` polls until `timeout_s` and then raises `UITimeoutError`.
- `keychord` accepts `["cmd", "return"]` or `["cmd+return"]`. `select_menu(["File", "New"])` clicks that menu-bar path.
- `metadata` fills `path`, bundle id (`CFBundleIdentifier`), and version (`CFBundleShortVersionString`) when `Contents/Info.plist` is readable. It does not require a live process. A plist that cannot be parsed leaves version empty and keeps `target.bundle_id`.

## Screenshots and video

Screenshots are PNGs under the worker media directory: `work_dir/media/<name>.png`. Capture uses `screencapture` (the front window rectangle when the tree reports one, otherwise the full display).

`start_video` / `stop_video`:

- `ffmpeg` on `PATH` records `work_dir/media/session.mp4` (avfoundation) until `stop_video`.
- Otherwise `screencapture` (including `/usr/sbin/screencapture` on a real Mac) records `session.mov`.
- If neither tool exists, `start_video` writes `work_dir/media/session.txt` noting that video is unavailable, and `stop_video` returns that path.

`close` stops an in-progress recording and quits the app.

## Linux tests

Every subprocess call goes through the instance attribute `runner` (default: a thin `subprocess` wrapper).

```python
driver = MacOSDriver(target, work_dir, runner=fake_runner, platform="darwin")
# or, after construction:
driver.runner = fake_runner
driver.platform = "darwin"
```

`platform` defaults to `sys.platform`. Leave it alone to assert the non-Mac error. Set it to `"darwin"` only when the fake runner is supplying launch and `osascript` results.

`runner(args, **kwargs)` matches the default wrapper:

- Foreground commands (`osascript -`, `open`, `which`, `/bin/sh -c`, `screencapture`, `kill`) return an object with `returncode`, `stdout`, and `stderr`.
- `osascript` reads the AppleScript from `kwargs["input"]`. Scripts are marked `SWARMQA_TREE`, `SWARMQA_ALIVE`, `SWARMQA_CLICK`, `SWARMQA_TYPE`, `SWARMQA_KEY`, `SWARMQA_SCROLL`, `SWARMQA_MENU`, and `SWARMQA_QUIT`.
- `background=True` (app executable, `ffmpeg`, `screencapture -v`) returns a handle with `pid`, `poll()`, `wait()`, and `kill()`. `poll()` returns `None` while the process is alive and an exit status after it crashes.

Tests in `tests/test_macos_driver.py` use that seam. They do not need a display, Accessibility permission, or macOS.

## iOS Simulator

`IOSSimulatorDriver` boots a Simulator, installs a simulator `.app`, and drives it. The module imports on Linux. `launch` when `platform` is not `darwin` raises `BackendUnavailable` and tells the caller to use the fake driver or a Mac with Xcode.

You need:

1. Xcode, with an iOS Simulator runtime installed. On this Mac that is Xcode 26.5 and the iOS 26.5 runtime. No device is booted until a campaign starts one.
2. [idb](https://github.com/facebook/idb) for the accessibility tree and taps. It is not part of Xcode and is not installed here. Homebrew 7 can install the current arm64 build (companion 1.6.2, which also installs the `idb` client):

```bash
brew tap facebook/fb
brew install facebook/fb/idb
```

The client needs Python 3.10 or newer. `pip install fb-idb` only installs the client; the companion still has to come from the brew formula.

`xcrun simctl` boots, installs, launches, screenshots, and records video. `simctl ui` only gets or sets appearance, Increase Contrast, and content size. It does not tap, type, or dump the accessibility tree. `devicectl` talks to devices Core Device already knows about; it is not a Simulator UI driver. `XCUIAutomation.framework` is the XCTest API, and this Xcode has no `xcuitest` command. UI actions stay on `idb`: tree, tap, text, key, and swipe. A missing `xcrun` fails at launch and names Xcode. A missing `idb` fails on the first UI action and names the install commands. Screenshots still use simctl.

### Which Simulator

Resolution order:

1. The `udid` argument on the driver.
2. `SWARMQA_SIMULATOR_UDID` in the environment.
3. A UDID in `app.simulator` or in the claimed `app.simulators` entry.
4. A device name. The default name is `iPhone 17`, which is the base phone on the iOS 26.5 runtime installed here. `iPhone 16` is not in that runtime. `simctl list devices available -j` picks a booted match, otherwise the same name on the newest runtime. Set `app.simulator` when you want a different device, such as `iPhone 17 Pro`.

`app.simulators` is the pool for concurrent workers. Each driver claims one entry with a lock next to the worker directory and releases it on `close`. A lock whose pid is gone is reused. When every entry is taken, launch raises `BackendUnavailable` and tells you to add one device per worker.

```toml
[app]
platform = "ios"
path = "/path/to/MyApp.app"
simulators = ["iPhone 17", "iPhone 17 Pro"]
```

Those names match `xcrun simctl list devices available` on the iOS 26.5 runtime. Create an extra device with `xcrun simctl create "SwarmQA 2" "iPhone 17"` and put that name, or the printed UDID, in the list. `close` terminates the app and leaves the Simulator booted.

One driver is one Simulator session. Two workers pointed at the same device will fight over the UI. The orchestrator warns when `workers` is greater than the number of `app.simulators` entries.

### Launch, tree, and actions

- `target.path` is an iOS simulator `.app`. A missing bundle raises `AppMissingError`.
- When `path` is empty, the driver runs `target.build_command` and reads a `.app` path from the command output.
- Bundle id comes from `CFBundleIdentifier` in `Info.plist` (the bundle root, or `Contents/Info.plist`). `app.bundle_id` fills in when the plist has none. Launch args are passed to `simctl launch`. `app.env` is passed as `SIMCTL_CHILD_*`.
- An exit during launch or a later action raises `AppCrashedError`. `simctl boot` reporting that the device is already booted is success.
- `accessibility_tree` maps idb JSON into `UIElement` nodes (role, label, identifier, value, enabled, frame, children). `XCUIElementTypeButton` becomes `button`.
- Element matching uses `swarmqa.driver.query`. `click` taps the center of the matched frame. `type_text` taps, then sends `idb ui text`. `scroll` swipes. Positive delta moves the finger up so content below comes into view. `select_menu(["Settings", "Account"])` taps each label in order. iOS has no macOS menu bar.
- `keychord` sends HID key codes through `idb ui key`. `return` is key 40. Modifier names (`cmd`, `ctrl`, `option`, `shift`) are dropped, and the remaining key is sent. An unknown key raises `ElementNotFoundError`.
- `wait_for` polls until `timeout_s`, then raises `UITimeoutError`.
- `relaunch` terminates and launches again. It does not reinstall.
- `metadata` fills path, bundle id, and `CFBundleShortVersionString`. `backend` is `ios`. It does not require a live process.

### Screenshots and video

Screenshots are PNGs at `work_dir/media/<name>.png` via `xcrun simctl io <udid> screenshot`.

`start_video` records `work_dir/media/session.mp4` with `simctl io recordVideo` (default codec hevc on this Xcode). `stop_video` sends SIGINT so simctl can finish the movie. If that process exits immediately, the driver writes `work_dir/media/session.txt` noting that video is unavailable, and `stop_video` returns that path.

### Linux tests

```python
driver = IOSSimulatorDriver(target, work_dir, runner=fake_runner, platform="darwin")
```

`platform` defaults to `sys.platform`. Set it to `"darwin"` only when the fake runner is supplying `xcrun` and `idb` results. Pass `udid` to skip device lookup.

`runner(args, **kwargs)` matches the macOS driver:

- Foreground commands (`xcrun simctl …`, `idb --udid …`, `which`, `/bin/sh -c`) return an object with `returncode`, `stdout`, and `stderr`.
- `background=True` (`simctl io recordVideo`) returns a handle with `pid`, `poll()`, `wait()`, and `kill()`.

Tests in `tests/test_ios_driver.py` use that seam. They do not need Xcode, idb, or a Simulator.
