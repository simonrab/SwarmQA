"""Linux tests for the macOS driver.

System Events is not available here. Tests inject ``driver.runner`` and set
``platform="darwin"`` when they need to exercise launch and osascript output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarmqa.driver import create_driver
from swarmqa.driver.macos import MacOSDriver, parse_accessibility_dump
from swarmqa.errors import (
    AppCrashedError,
    AppMissingError,
    BackendUnavailable,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import AppTarget, ElementQuery
from swarmqa.testing import make_app

# 1x1 RGB PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
)


def row(
    depth: int,
    role: str,
    label: str = "",
    identifier: str = "",
    value: str = "",
    enabled: str = "1",
    x: float = 0,
    y: float = 0,
    w: float = 0,
    h: float = 0,
) -> str:
    return "\t".join(
        [
            str(depth),
            role,
            label,
            identifier,
            value,
            enabled,
            str(x),
            str(y),
            str(w),
            str(h),
        ]
    )


SAMPLE_TREE = "\n".join(
    [
        row(0, "AXWindow", "Sample", "", "", "1", 0, 0, 800, 600),
        row(1, "AXGroup", "Body"),
        row(2, "AXButton", "Save Draft", "save", "", "1", 10, 10, 80, 30),
        row(2, "AXTextField", "Email", "", "user@example.com", "1", 10, 50, 220, 24),
        row(2, "AXButton", "Dark Mode", "", "", "0", 10, 90, 90, 28),
        row(0, "AXMenuBar", "Menu Bar"),
        row(1, "AXMenu", "File"),
        row(1, "AXMenuItem", "New"),
    ]
)


class FakeProcess:
    def __init__(self, pid: int = 4242, code: int | None = None):
        self.pid = pid
        self.code = code
        self.signals: list[str] = []

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int | None:
        if self.code is None:
            self.code = 0
        return self.code

    def kill(self) -> None:
        self.signals.append("kill")
        self.code = -9


class FakeRunner:
    """Stand-in for osascript, open, screencapture, and ffmpeg."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.kw: list[dict] = []
        self.tree = SAMPLE_TREE
        self.alive = True
        self.tools: dict[str, str] = {}
        self.build_code = 0
        self.build_stdout = ""
        self.build_stderr = ""
        self.open_code = 0
        self.open_stderr = ""
        self.crash_on_launch = False
        self.action_code = 0
        self.action_stderr = ""
        self.binary_exit: int | None = None
        self.processes: list[FakeProcess] = []

    def __call__(self, args: list[str], **kwargs):
        args = list(args)
        self.calls.append(args)
        self.kw.append(kwargs)
        program = Path(args[0]).name
        if program == "which":
            found = self.tools.get(args[1])
            if not found:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=found + "\n", stderr="")
        if args[:2] == ["/bin/sh", "-c"]:
            return subprocess.CompletedProcess(
                args,
                self.build_code,
                stdout=self.build_stdout,
                stderr=self.build_stderr,
            )
        if program == "open":
            self.alive = not self.crash_on_launch
            return subprocess.CompletedProcess(args, self.open_code, stdout="", stderr=self.open_stderr)
        if kwargs.get("background"):
            code = None
            if program not in {"ffmpeg", "screencapture"}:
                code = self.binary_exit
                self.alive = code is None
            proc = FakeProcess(code=code)
            self.processes.append(proc)
            return proc
        if program == "osascript":
            return self._script(args, kwargs.get("input") or "")
        if program == "screencapture":
            if "-v" in args:
                proc = FakeProcess()
                self.processes.append(proc)
                return proc
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PNG)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if program == "kill":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _script(self, args: list[str], script: str) -> subprocess.CompletedProcess:
        if "SWARMQA_ALIVE" in script:
            if not self.alive:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="process exited")
            return subprocess.CompletedProcess(args, 0, stdout="4242\n", stderr="")
        if "SWARMQA_TREE" in script:
            if not self.alive:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="process exited")
            return subprocess.CompletedProcess(args, 0, stdout=self.tree, stderr="")
        if "SWARMQA_QUIT" in script:
            self.alive = False
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if self.action_code != 0:
            return subprocess.CompletedProcess(
                args,
                self.action_code,
                stdout="",
                stderr=self.action_stderr or "action failed",
            )
        if not self.alive:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="process exited")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def scripts(self) -> list[str]:
        found = []
        for kwargs in self.kw:
            script = kwargs.get("input")
            if isinstance(script, str) and script:
                found.append(script)
        return found


def darwin_driver(tmp_path: Path, runner: FakeRunner, target: AppTarget | None = None) -> MacOSDriver:
    app = target if target is not None else make_app(tmp_path)
    return MacOSDriver(app, tmp_path / "work", runner=runner, platform="darwin")


def test_import_and_factory_work_on_linux(tmp_path: Path):
    import swarmqa.driver.macos as macos

    assert macos.MacOSDriver is MacOSDriver
    driver = create_driver(make_app(tmp_path), tmp_path / "work", kind="macos")
    assert isinstance(driver, MacOSDriver)


def test_launch_on_non_darwin_tells_caller_to_use_fake_or_mac(tmp_path: Path):
    driver = MacOSDriver(make_app(tmp_path), tmp_path / "work", platform="linux")
    assert driver.platform != "darwin"
    with pytest.raises(BackendUnavailable) as exc:
        driver.launch()
    message = str(exc.value)
    assert "fake driver" in message
    assert "Mac host" in message
    with pytest.raises(BackendUnavailable) as relaunch:
        driver.relaunch()
    assert "fake driver" in str(relaunch.value)
    driver.close()


def test_runner_attribute_is_the_subprocess_seam(tmp_path: Path):
    driver = MacOSDriver(make_app(tmp_path), tmp_path / "work", platform="darwin")
    runner = FakeRunner()
    driver.runner = runner
    driver.launch()
    assert any(call[0] == "open" for call in runner.calls)
    assert driver.launched


def test_missing_bundle_raises_before_open(tmp_path: Path):
    runner = FakeRunner()
    target = AppTarget(path=str(tmp_path / "Missing.app"), bundle_id="dev.missing")
    driver = darwin_driver(tmp_path, runner, target)
    with pytest.raises(AppMissingError) as exc:
        driver.launch()
    assert "Missing.app" in str(exc.value)
    assert runner.calls == []


def test_open_failure_and_unsigned_bundle(tmp_path: Path):
    runner = FakeRunner()
    runner.open_code = 1
    runner.open_stderr = "code signature invalid: unsigned app"
    driver = darwin_driver(tmp_path, runner)
    with pytest.raises(AppMissingError) as exc:
        driver.launch()
    assert "unsigned" in str(exc.value)


def test_launch_uses_open_when_executable_is_absent(tmp_path: Path):
    runner = FakeRunner()
    app = make_app(tmp_path)
    app.launch_args = ["--debug"]
    app.env = {"SAMPLE": "1"}
    driver = darwin_driver(tmp_path, runner, app)
    driver.launch()
    opened = runner.calls[0]
    assert opened[:3] == ["open", "-n", app.path]
    assert opened[-2:] == ["--args", "--debug"]
    assert runner.kw[0]["env"]["SAMPLE"] == "1"
    assert driver.launched
    assert any("SWARMQA_ALIVE" in script for script in runner.scripts())


def test_launch_runs_build_command_when_path_is_empty(tmp_path: Path):
    built = make_app(tmp_path, "Built.app")
    runner = FakeRunner()
    runner.build_stdout = f"built {built.path}\n"
    target = AppTarget(path="", build_command="xcodebuild -scheme Built", bundle_id="dev.swarmqa.sample")
    driver = darwin_driver(tmp_path, runner, target)
    driver.launch()
    assert runner.calls[0] == ["/bin/sh", "-c", "xcodebuild -scheme Built"]
    assert any(call[0] == "open" and call[2] == built.path for call in runner.calls)
    meta = driver.metadata()
    assert meta.path == built.path
    assert meta.bundle_id == "dev.swarmqa.sample"
    assert meta.version == "0.0.1"


def test_build_failure_and_missing_product(tmp_path: Path):
    runner = FakeRunner()
    runner.build_code = 1
    runner.build_stderr = "compile error"
    target = AppTarget(path=None, build_command="xcodebuild")
    driver = darwin_driver(tmp_path, runner, target)
    with pytest.raises(AppMissingError) as exc:
        driver.launch()
    assert "compile error" in str(exc.value)

    runner.build_code = 0
    runner.build_stderr = ""
    runner.build_stdout = "ok\n"
    with pytest.raises(AppMissingError) as missing:
        driver.launch()
    assert ".app" in str(missing.value)


def test_configured_path_skips_build_command(tmp_path: Path):
    runner = FakeRunner()
    app = make_app(tmp_path)
    app.build_command = "echo should-not-run"
    driver = darwin_driver(tmp_path, runner, app)
    driver.launch()
    assert not any(call[:2] == ["/bin/sh", "-c"] for call in runner.calls)


def test_binary_launch_and_crash_on_exit(tmp_path: Path):
    app = make_app(tmp_path)
    binary = Path(app.path) / "Contents" / "MacOS" / "Sample"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner, app)
    driver.launch()
    assert runner.calls[0][0] == str(binary)
    assert runner.kw[0]["background"] is True
    assert not any(call[0] == "open" for call in runner.calls)

    crashed = FakeRunner()
    crashed.binary_exit = 9
    driver = darwin_driver(tmp_path, crashed, app)
    with pytest.raises(AppCrashedError) as exc:
        driver.launch()
    assert "status 9" in str(exc.value)


def test_open_reports_crash_when_process_exits_immediately(tmp_path: Path):
    runner = FakeRunner()
    runner.crash_on_launch = True
    driver = darwin_driver(tmp_path, runner)
    with pytest.raises(AppCrashedError):
        driver.launch()
    assert driver.launched is False


def test_accessibility_tree_normalizes_roles_and_nesting(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    tree = driver.accessibility_tree()
    assert [node.role for node in tree] == ["window", "menubar"]
    window = tree[0]
    assert window.label == "Sample"
    assert window.frame == (0.0, 0.0, 800.0, 600.0)
    group = window.children[0]
    assert group.role == "group"
    button = group.children[0]
    assert button.role == "button"
    assert button.label == "Save Draft"
    assert button.identifier == "save"
    assert button.enabled is True
    dark = group.children[2]
    assert dark.label == "Dark Mode"
    assert dark.enabled is False
    email = group.children[1]
    assert email.role == "textfield"
    assert email.value == "user@example.com"
    assert tree[1].children[0].role == "menu"
    assert tree[1].children[1].role == "menuitem"


def test_click_type_and_query_semantics(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    driver.click(ElementQuery(role="BUTTON", label="save"))
    click = next(script for script in runner.scripts() if "SWARMQA_CLICK" in script)
    assert "Save Draft" in click
    assert "identifier=save" in click
    assert "AXButton" in click

    driver.type_text(ElementQuery(role="textfield", label="EMAIL"), 'say "hi"')
    typed = next(script for script in runner.scripts() if "SWARMQA_TYPE" in script)
    assert "Email" in typed
    assert r'say \"hi\"' in typed

    driver.click(ElementQuery(identifier="save"))
    with pytest.raises(ElementNotFoundError):
        driver.click(ElementQuery(role="button", label="Missing"))
    with pytest.raises(ElementNotFoundError):
        driver.click(ElementQuery(role="menu", label="save"))
    with pytest.raises(ElementNotFoundError):
        driver.click(ElementQuery(identifier="save-x"))
    with pytest.raises(ElementNotFoundError):
        driver.type_text(ElementQuery(role="textfield", label="Email", value="nope"), "x")
    driver.type_text(
        ElementQuery(role="textfield", label="Email", value="user@example.com"),
        "next",
    )


def test_keychord_scroll_and_menu(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    driver.keychord(["cmd", "return"])
    driver.keychord(["cmd+shift+n"])
    keys = [script for script in runner.scripts() if "SWARMQA_KEY" in script]
    assert "key code 36" in keys[0]
    assert "command down" in keys[0]
    assert 'keystroke "n"' in keys[1]
    assert "command down" in keys[1]
    assert "shift down" in keys[1]
    with pytest.raises(Exception) as empty:
        driver.keychord(["cmd"])
    assert "keychord" in str(empty.value)

    driver.scroll(-3)
    scrolled = next(script for script in runner.scripts() if "SWARMQA_SCROLL" in script)
    assert "delta=-3" in scrolled
    assert "key code 126" in scrolled
    assert "repeat 3 times" in scrolled

    driver.scroll(2, ElementQuery(role="button", label="Dark"))
    assert any("SWARMQA_CLICK" in script and "Dark Mode" in script for script in runner.scripts())
    with pytest.raises(ElementNotFoundError):
        driver.scroll(1, ElementQuery(role="button", label="Missing"))

    driver.select_menu(["File", "New"])
    driver.select_menu(["File", "Recent", "Doc"])
    menus = [script for script in runner.scripts() if "SWARMQA_MENU" in script]
    assert 'menu item "New" of menu 1 of menu bar item "File" of menu bar 1' in menus[0]
    assert (
        'menu item "Doc" of menu 1 of menu item "Recent" of menu 1 of menu bar item "File" of menu bar 1'
        in menus[1]
    )
    with pytest.raises(ElementNotFoundError):
        driver.select_menu([])


def test_wait_for_timeout_and_match(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver._poll_s = 0
    driver.launch()
    found = driver.wait_for(ElementQuery(role="button", label="draft"), 1)
    assert found.identifier == "save"
    with pytest.raises(UITimeoutError) as exc:
        driver.wait_for(ElementQuery(role="button", label="Missing"), 0)
    assert "timed out after 0" in str(exc.value)


def test_actions_require_a_live_process(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    with pytest.raises(AppMissingError):
        driver.click(ElementQuery(role="button", label="Save"))
    driver.launch()
    runner.alive = False
    with pytest.raises(AppCrashedError):
        driver.click(ElementQuery(role="button", label="save"))

    runner.alive = True
    runner.action_code = 1
    runner.action_stderr = "process exited"
    with pytest.raises(AppCrashedError):
        driver.keychord(["return"])

    runner.action_stderr = "osascript is not allowed assistive access."
    with pytest.raises(BackendUnavailable) as exc:
        driver.keychord(["return"])
    assert "Accessibility" in str(exc.value)


def test_screenshot_is_png_under_worker_media(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    shot = driver.screenshot("after click")
    assert shot.parent == driver.work_dir / "media"
    assert shot.name == "after-click.png"
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert any(Path(call[0]).name == "screencapture" for call in runner.calls)


def test_video_note_when_tools_are_missing(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    assert driver.stop_video() is None
    driver.start_video()
    note = driver.stop_video()
    assert note is not None
    assert note.name == "session.txt"
    assert note.parent.name == "media"
    text = note.read_text(encoding="utf-8")
    assert "unavailable" in text
    assert "ffmpeg" in text
    assert "screencapture" in text
    assert not any(Path(call[0]).name == "ffmpeg" for call in runner.calls)


def test_video_prefers_ffmpeg_then_screencapture(tmp_path: Path):
    runner = FakeRunner()
    runner.tools = {
        "ffmpeg": "/usr/bin/ffmpeg",
        "screencapture": "/usr/sbin/screencapture",
    }
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    driver.start_video()
    driver.start_video()
    ffmpeg_calls = [call for call in runner.calls if Path(call[0]).name == "ffmpeg"]
    assert len(ffmpeg_calls) == 1
    assert "avfoundation" in ffmpeg_calls[0]
    path = driver.stop_video()
    assert path is not None
    assert path.name == "session.mp4"
    assert any(call[:2] == ["kill", "-INT"] for call in runner.calls)

    capture_runner = FakeRunner()
    capture_runner.tools = {"screencapture": "/usr/sbin/screencapture"}
    capture_driver = darwin_driver(tmp_path, capture_runner)
    capture_driver.launch()
    capture_driver.start_video()
    video = capture_driver.stop_video()
    assert video is not None
    assert video.name == "session.mov"
    assert any(
        Path(call[0]).name == "screencapture" and "-v" in call for call in capture_runner.calls
    )


def test_metadata_reads_info_plist_without_launch(tmp_path: Path):
    app = make_app(tmp_path)
    driver = MacOSDriver(app, tmp_path / "work")
    meta = driver.metadata()
    assert meta.path == app.path
    assert meta.bundle_id == "dev.swarmqa.sample"
    assert meta.version == "0.0.1"
    assert meta.backend == "macos"

    plist = Path(app.path) / "Contents" / "Info.plist"
    plist.write_text("not a plist", encoding="utf-8")
    broken = driver.metadata()
    assert broken.version is None
    assert broken.bundle_id == "dev.swarmqa.sample"
    assert broken.path == app.path


def test_relaunch_quits_and_launches_again(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    driver.relaunch()
    assert driver.launched
    assert [call for call in runner.calls if call[0] == "open"].__len__() == 2
    assert any("SWARMQA_QUIT" in script for script in runner.scripts())
    driver.close()
    driver.close()
    assert driver.launched is False
    assert sum("SWARMQA_QUIT" in script for script in runner.scripts()) == 2


def test_close_stops_video_note(tmp_path: Path):
    runner = FakeRunner()
    driver = darwin_driver(tmp_path, runner)
    driver.launch()
    driver.start_video()
    driver.close()
    assert driver._video_on is False
    note = driver.stop_video()
    assert note is not None and note.name == "session.txt"


def test_parse_ignores_noise_lines():
    text = "\n".join(["-- comment", "not a row", row(0, "button", "Save")])
    nodes = parse_accessibility_dump(text)
    assert len(nodes) == 1
    assert nodes[0].role == "button"
    assert nodes[0].label == "Save"
