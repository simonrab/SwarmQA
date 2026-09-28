# macOS app driver

`MacOSDriver` (`swarmqa.driver.macos`) is the real session driver for a macOS `.app`. It satisfies `AppDriver`: launch, relaunch, accessibility tree, click, type, keychord, scroll, menu select, wait, screenshot, video, and build metadata.

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
