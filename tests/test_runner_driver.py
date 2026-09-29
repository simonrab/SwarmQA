"""Runner drivers (WP-B1) against an in-process fake swarm runner.

The fake runner is a stdlib HTTP server on 127.0.0.1 that answers the wire
protocol with `runner_schema` encoders. `FakeSpawn` stands in for xcodebuild:
it starts that server on the port xcodebuild would pass as
TEST_RUNNER_SWARM_RUNNER_PORT. `FakeCommands` answers simctl and friends.
Nothing here needs macOS or Xcode.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from swarmqa.driver import create_driver
from swarmqa.driver import runner_schema as schema
from swarmqa.driver.ios_runner import IOSRunnerDriver
from swarmqa.driver.macos_runner import MacOSRunnerDriver
from swarmqa.driver.protocol import supports_v2
from swarmqa.driver.runner_client import (
    AUTOMATION_MODE_TEXT,
    AX_LOADED_TEXT,
    IOS_RUNNER_BUNDLE_ID,
    RunnerClient,
    RunnerProcess,
    RunnerTimeouts,
    driver_error,
    find_xctestrun,
    normalise_keys,
    parse_log_ndjson,
)
from swarmqa.errors import (
    AQAError,
    AppCrashedError,
    AppMissingError,
    BackendUnavailable,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import AppTarget, ElementQuery, UIElement

UDID = "11111111-2222-3333-4444-555555555555"
OTHER_UDID = "99999999-8888-7777-6666-555555555555"
BUNDLE_ID = "dev.swarmqa.PlantedBugs"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
)
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


# Fake runner


def _tree() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="PlantedBugs",
            frame=(0, 0, 402, 874),
            children=[
                UIElement(role="button", label="Report", identifier="home.row.report", frame=(16, 200, 370, 44)),
                UIElement(role="button", label="Scan", identifier="attachments.row.scan", frame=(16, 260, 370, 44)),
            ],
        )
    ]


class RunnerState:
    def __init__(self, platform: str = "ios"):
        self.platform = platform
        self.app_state = "not_running"
        self.installed = {BUNDLE_ID}
        self.launched: str | None = None
        self.tree = _tree()
        self.crash_on = {"attachments.row.scan"}
        self.requests: list[tuple[str, str, Any]] = []
        self.connections = 0
        self.delay: dict[str, float] = {}
        self.fail: dict[str, schema.RunnerError] = {}
        self.close_after_each = False

    def match(self, query: dict) -> UIElement | None:
        from swarmqa.driver.query import find_element

        try:
            return find_element(self.tree, schema.decode_query(query))
        except ElementNotFoundError:
            return None

    def handle(self, method: str, path: str, body: Any) -> tuple[int, str, bytes]:
        self.requests.append((method, path, body))
        if path in self.delay:
            time.sleep(self.delay[path])
        if path in self.fail:
            error = self.fail[path]
            return self._error(error)
        if path == "/health":
            return self._ok({"protocol_version": 1, "runner_version": "fake", "platform": self.platform, "app_state": self.app_state})
        if path == "/launch":
            bundle = body.get("bundle_id")
            if bundle not in self.installed:
                return self._error(schema.RunnerError("bad_request", f"{bundle} is not installed"))
            self.launched = bundle
            self.app_state = "running"
            return self._ok({"ok": True})
        if path == "/terminate":
            self.app_state = "not_running"
            return self._ok({"ok": True})
        if path == "/screenshot":
            if self.launched is None:
                return self._error(schema.RunnerError("not_launched", "no app"))
            return 200, "image/png", PNG
        if self.launched is None:
            return self._error(schema.RunnerError("not_launched", "no app has been launched"))
        if self.app_state == "crashed":
            return self._error(schema.RunnerError("app_crashed", f"{self.launched} is no longer running"))
        if path == "/tree":
            return self._ok({"ts": time.time(), "elements": [schema.encode_element(e) for e in self.tree]})
        if path == "/observe":
            fmt = body.get("screenshot")
            shot = None
            if fmt == "png":
                shot = schema.Screenshot("png", PNG)
            elif fmt == "jpeg":
                shot = schema.Screenshot("jpeg", JPEG)
            response = schema.ObserveResponse(ts=time.time(), elements=self.tree, size=(402.0, 874.0), scale=3.0, screenshot=shot)
            return self._ok(schema.encode_observe_response(response))
        if path == "/tap":
            if "query" in body:
                element = self.match(body["query"])
                if element is None:
                    return self._error(schema.RunnerError("not_found", f"no element matches {json.dumps(body['query'])}"))
                if element.identifier in self.crash_on:
                    self.app_state = "crashed"
            return self._ok({"ok": True})
        if path in ("/type", "/swipe", "/key"):
            return self._ok({"ok": True})
        return self._error(schema.RunnerError("bad_request", f"unknown endpoint {path}"))

    def _ok(self, obj: Any) -> tuple[int, str, bytes]:
        return 200, "application/json", json.dumps(obj).encode()

    def _error(self, error: schema.RunnerError) -> tuple[int, str, bytes]:
        return schema.ERROR_STATUS[error.code], "application/json", json.dumps(schema.encode_error(error)).encode()


class FakeRunnerServer:
    def __init__(self, state: RunnerState, port: int = 0):
        self.state = state
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        state = self.state
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 5

            def setup(self):
                super().setup()
                state.connections += 1

            def log_message(self, *args):
                return

            def _serve(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get(schema.PROTOCOL_HEADER) != "1":
                    status, ctype, data = state._error(schema.RunnerError("protocol_mismatch", "header"))
                else:
                    body = json.loads(raw) if raw else None
                    with lock:  # the runner serves one request at a time
                        status, ctype, data = state.handle(method, self.path, body)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if state.close_after_each:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        return Handler

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeProc:
    def __init__(self, server: FakeRunnerServer | None, code: int | None = None):
        self.server = server
        self.code = code
        self.pid = 4242
        self.signals: list[int] = []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig in (signal.SIGTERM, signal.SIGKILL, signal.SIGINT) and self.code is None:
            if self.server is not None:
                self.server.stop()
                self.server = None
            self.code = -sig


class FakeSpawn:
    """Stands in for xcodebuild. `script` holds per-attempt behaviours."""

    def __init__(self, state: RunnerState, script: list[str] | None = None):
        self.state = state
        self.script = list(script or [])
        self.calls: list[tuple[list[str], dict]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, args, *, env, log_path):
        self.calls.append((list(args), dict(env)))
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if args and args[0] != "xcodebuild":  # video recorder
            proc = FakeProc(None)
            self.procs.append(proc)
            return proc
        mode = self.script.pop(0) if self.script else "ok"
        if mode == "ax":
            log_path.write_text(f"Testing started\n{AX_LOADED_TEXT}\n** TEST EXECUTE FAILED **\n")
            proc = FakeProc(None, code=65)
        elif mode == "automation":
            log_path.write_text(f"{AUTOMATION_MODE_TEXT}.\n")
            proc = FakeProc(None, code=None)
        elif mode == "hang":
            log_path.write_text("Testing started\n")
            proc = FakeProc(None, code=None)
        else:
            port = int(env["TEST_RUNNER_SWARM_RUNNER_PORT"])
            server = FakeRunnerServer(self.state, port)
            log_path.write_text(f"SWARM_RUNNER_READY port={port} platform={self.state.platform} protocol=1\n")
            proc = FakeProc(server)
        self.procs.append(proc)
        return proc

    def stop_all(self):
        for proc in self.procs:
            if proc.server is not None:
                proc.server.stop()
                proc.server = None


class FakeCommands:
    def __init__(self, responses: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}
        self.on_call = None

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if self.on_call is not None:
            self.on_call(args, kwargs)
        joined = " ".join(args)
        for prefix, (code, out, err) in self.responses.items():
            if joined.startswith(prefix):
                return subprocess.CompletedProcess(args, code, out, err)
        return subprocess.CompletedProcess(args, 0, "", "")

    def ran(self, prefix: str) -> list[list[str]]:
        return [call for call in self.calls if " ".join(call).startswith(prefix)]


# Fixtures


@pytest.fixture
def derived(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("SWARM_RUNNER_XCTESTRUN", raising=False)
    monkeypatch.delenv("SWARM_RUNNER_RESULT_DIR", raising=False)
    monkeypatch.delenv("SWARMQA_SIMULATOR_UDID", raising=False)
    root = tmp_path / "derived"
    products = root / "Build" / "Products"
    products.mkdir(parents=True)
    (products / "SwarmRunner-iOS_iphonesimulator26.5-arm64.xctestrun").write_text("<plist/>")
    (products / "SwarmRunner-macOS_macosx26.5-arm64.xctestrun").write_text("<plist/>")
    monkeypatch.setenv("SWARM_RUNNER_DERIVED_DATA", str(root))
    return root


def _app(tmp_path: Path, name: str = "PlantedBugs", macos: bool = False) -> Path:
    import plistlib

    bundle = tmp_path / "apps" / f"{name}.app"
    plist_dir = bundle / "Contents" if macos else bundle
    plist_dir.mkdir(parents=True)
    with (plist_dir / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": name, "CFBundleShortVersionString": "1.0"},
            handle,
        )
    return bundle


def _ios_driver(tmp_path, state=None, script=None, **kwargs):
    state = state or RunnerState("ios")
    spawn = FakeSpawn(state, script)
    cmds = kwargs.pop("commands", None) or FakeCommands()
    target = AppTarget(path=str(_app(tmp_path)), platform="ios", **kwargs.pop("target", {}))
    driver = IOSRunnerDriver(
        target,
        tmp_path / "work",
        runner=cmds,
        spawn=spawn,
        platform="darwin",
        crash_dir=tmp_path / "crashes",
        crash_report_wait_s=0.0,
        sleep=lambda s: time.sleep(min(s, 0.01)),
        timeouts=RunnerTimeouts(health=1.0, launch=5.0, warmup=5.0, step=5.0, observe=5.0),
        **kwargs,
    )
    return driver, state, spawn, cmds


def _write_ips(folder: Path, name: str, proc_path: str, *, mtime: float | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.ips"
    header = {"app_name": "PlantedBugs", "bundleID": BUNDLE_ID, "incident_id": "ABC", "os_version": "macOS 26.5"}
    body = {
        "procName": "PlantedBugs",
        "procPath": proc_path,
        "captureTime": "2026-09-29 20:49:01.5260 +0100",
        "exception": {"type": "EXC_BREAKPOINT", "signal": "SIGTRAP"},
        "termination": {"indicator": "Trace/BPT trap: 5"},
    }
    path.write_text(json.dumps(header) + "\n" + json.dumps(body, indent=2))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# Client


@pytest.fixture
def server():
    state = RunnerState("ios")
    srv = FakeRunnerServer(state)
    yield srv
    srv.stop()


def test_client_health_launch_observe(server):
    client = RunnerClient(server.port)
    health = client.health()
    assert health.platform == "ios" and health.app_state == "not_running" and health.protocol_version == 1
    client.launch(BUNDLE_ID, args=["-x"], env={"A": "1"})
    assert server.state.requests[-1] == (
        "POST",
        "/launch",
        {"bundle_id": BUNDLE_ID, "args": ["-x"], "env": {"A": "1"}, "terminate_existing": True},
    )
    response = client.observe("png")
    assert response.screenshot is not None and response.screenshot.data == PNG
    assert response.size == (402.0, 874.0) and response.scale == 3.0
    assert response.elements[0].children[0].identifier == "home.row.report"
    ts, tree = client.tree()
    assert ts > 0 and tree[0].role == "window"
    assert client.screenshot_png() == PNG
    # One keep-alive connection for every call.
    assert server.state.connections == 1


def test_client_tap_bodies(server):
    client = RunnerClient(server.port)
    client.launch(BUNDLE_ID)
    client.tap(query=ElementQuery(identifier="home.row.report"))
    client.tap(point=(10, 20.5))
    client.swipe((1, 2), (3, 4), 0.5)
    client.key(["cmd", "s"])
    client.type_text("hi", ElementQuery(label="Name"))
    bodies = [body for _, path, body in server.state.requests if path in ("/tap", "/swipe", "/key", "/type")]
    assert bodies == [
        {"query": {"role": None, "label": None, "identifier": "home.row.report", "value": None}},
        {"x": 10.0, "y": 20.5},
        {"from": [1.0, 2.0], "to": [3.0, 4.0], "duration_s": 0.5},
        {"keys": ["cmd", "s"]},
        {"text": "hi", "query": {"role": None, "label": "Name", "identifier": None, "value": None}},
    ]


@pytest.mark.parametrize(
    "code,expected",
    [
        ("app_crashed", AppCrashedError),
        ("not_launched", AppMissingError),
        ("not_found", ElementNotFoundError),
        ("timeout", UITimeoutError),
        ("protocol_mismatch", BackendUnavailable),
        ("internal", AQAError),
        ("bad_request", AQAError),
    ],
)
def test_error_codes_map_to_driver_errors(server, code, expected):
    server.state.fail["/tap"] = schema.RunnerError(code, "boom")
    client = RunnerClient(server.port)
    with pytest.raises(expected) as info:
        client.tap(point=(1, 1))
    assert info.value.code == code
    assert isinstance(info.value.__cause__, schema.RunnerError)
    assert type(info.value) is expected


def test_not_installed_is_app_missing(server):
    client = RunnerClient(server.port)
    with pytest.raises(AppMissingError, match="dev.example.Missing is not installed"):
        client.launch("dev.example.Missing")


def test_crash_then_relaunch_recovers(server):
    client = RunnerClient(server.port)
    client.launch(BUNDLE_ID)
    client.tap(query=ElementQuery(identifier="attachments.row.scan"))
    with pytest.raises(AppCrashedError):
        client.observe()
    assert client.health().app_state == "crashed"
    client.launch(BUNDLE_ID)
    server.state.app_state = "running"
    assert client.observe("none").screenshot is None


def test_client_timeout_raises_ui_timeout(server):
    server.state.delay["/tree"] = 0.5
    client = RunnerClient(server.port)
    client.launch(BUNDLE_ID)
    with pytest.raises(UITimeoutError):
        client.tree(timeout=0.1)
    time.sleep(0.5)
    server.state.delay.clear()
    assert client.tree()[1]


def test_client_reconnects_when_server_closes(server):
    server.state.close_after_each = True
    client = RunnerClient(server.port)
    client.health()
    client.health()
    assert server.state.connections == 2


def test_client_unreachable_is_backend_unavailable():
    from swarmqa.driver.runner_client import free_port

    client = RunnerClient(free_port())
    with pytest.raises(BackendUnavailable, match="not reachable"):
        client.health()


def test_driver_error_screen_locked():
    err = driver_error(schema.RunnerError("bad_request", "Failed to activate application (current state: Running Background)"))
    assert isinstance(err, BackendUnavailable) and "unlocked" in str(err)


def test_normalise_keys():
    assert normalise_keys(["cmd+s"]) == ["cmd", "s"]
    assert normalise_keys(["Command", "Option", "Enter", "esc", "S"]) == ["cmd", "alt", "return", "escape", "S"]


# RunnerProcess


def _process(tmp_path, platform="ios", script=None, **kwargs):
    state = kwargs.pop("state", None) or RunnerState(platform)
    spawn = FakeSpawn(state, script)
    cmds = kwargs.pop("commands", None) or FakeCommands()
    proc = RunnerProcess(
        platform,
        tmp_path / "work",
        udid=UDID if platform == "ios" else None,
        runner=cmds,
        spawn=spawn,
        poll_s=0.01,
        timeouts=RunnerTimeouts(health=0.5),
        **kwargs,
    )
    return proc, spawn, cmds, state


def test_process_start_command_and_health(tmp_path, derived):
    proc, spawn, cmds, _ = _process(tmp_path)
    try:
        client = proc.start()
        assert client.health().platform == "ios"
        args, env = spawn.calls[0]
        assert args[:2] == ["xcodebuild", "test-without-building"]
        assert args[args.index("-xctestrun") + 1].endswith("SwarmRunner-iOS_iphonesimulator26.5-arm64.xctestrun")
        assert args[args.index("-destination") + 1] == f"platform=iOS Simulator,id={UDID}"
        assert args[args.index("-only-testing") + 1] == "SwarmRunnerUITests-iOS/SwarmRunnerTests/testRunServer"
        assert args[args.index("-test-timeouts-enabled") + 1] == "NO"
        assert args[args.index("-resultBundlePath") + 1].endswith(".xcresult")
        assert env["TEST_RUNNER_SWARM_RUNNER_PORT"] == str(proc.port)
        assert proc.log_path is not None and proc.log_path.parent == tmp_path / "work" / "runner"
        assert "SWARM_RUNNER_READY" in proc.log_tail()
    finally:
        proc.stop()
    assert spawn.procs[0].signals[0] == signal.SIGTERM
    assert ["xcrun", "simctl", "terminate", UDID, IOS_RUNNER_BUNDLE_ID] in cmds.calls
    proc.stop()  # idempotent


def test_process_retries_once_after_ax_loaded_failure(tmp_path, derived):
    proc, spawn, _, _ = _process(tmp_path, script=["ax", "ok"])
    try:
        proc.start()
        assert proc.start_attempts == 2
        assert len(spawn.calls) == 2
    finally:
        proc.stop()


def test_process_gives_up_after_two_failures(tmp_path, derived):
    proc, spawn, _, _ = _process(tmp_path, script=["ax", "ax", "ok"])
    with pytest.raises(BackendUnavailable, match="after 2 attempts"):
        proc.start()
    assert len(spawn.calls) == 2


def test_process_readiness_timeout(tmp_path, derived):
    proc, spawn, _, _ = _process(tmp_path, script=["hang", "hang"], ready_timeout_s=0.2)
    with pytest.raises(BackendUnavailable, match="did not answer /health"):
        proc.start()
    assert len(spawn.calls) == 2
    assert all(signal.SIGTERM in p.signals for p in spawn.procs)


def test_process_automation_mode_is_not_retried(tmp_path, derived):
    proc, spawn, _, _ = _process(tmp_path, platform="macos", script=["automation", "ok"])
    with pytest.raises(BackendUnavailable) as info:
        proc.start()
    assert "automationmodetool enable-automationmode-without-authentication" in str(info.value)
    assert "approve" in str(info.value)
    assert len(spawn.calls) == 1


def test_process_macos_destination(tmp_path, derived):
    proc, spawn, _, _ = _process(tmp_path, platform="macos", port=None)
    try:
        proc.start()
        args, _ = spawn.calls[0]
        assert args[args.index("-destination") + 1].startswith("platform=macOS,arch=")
        assert "SwarmRunnerUITests-macOS/SwarmRunnerTests/testRunServer" in args
    finally:
        proc.stop()


def test_missing_build_without_auto_build(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_RUNNER_XCTESTRUN", raising=False)
    monkeypatch.setenv("SWARM_RUNNER_DERIVED_DATA", str(tmp_path / "empty"))
    proc, spawn, _, _ = _process(tmp_path, auto_build=False)
    with pytest.raises(BackendUnavailable, match=r"agents/swarm-runner/build.sh ios"):
        proc.start()
    assert spawn.calls == []


def test_missing_build_runs_build_script_once(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_RUNNER_XCTESTRUN", raising=False)
    root = tmp_path / "fresh"
    monkeypatch.setenv("SWARM_RUNNER_DERIVED_DATA", str(root))
    cmds = FakeCommands()

    def build(args, kwargs):
        if args[0].endswith("build.sh"):
            assert kwargs["env"]["SWARM_RUNNER_DERIVED_DATA"] == str(root)
            products = root / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "SwarmRunner-iOS_iphonesimulator26.5-arm64.xctestrun").write_text("x")

    cmds.on_call = build
    proc, spawn, _, _ = _process(tmp_path, commands=cmds)
    try:
        proc.start()
    finally:
        proc.stop()
    builds = [call for call in cmds.calls if call[0].endswith("build.sh")]
    assert builds and builds[0][1] == "ios"
    assert len(builds) == 1


def test_build_failure_is_clear(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_RUNNER_XCTESTRUN", raising=False)
    monkeypatch.setenv("SWARM_RUNNER_DERIVED_DATA", str(tmp_path / "none"))
    cmds = FakeCommands({"/": (1, "", "error: no such scheme")})
    proc, spawn, _, _ = _process(tmp_path, commands=cmds)
    with pytest.raises(BackendUnavailable, match="building the swarm runner failed"):
        proc.start()


def test_find_xctestrun_prefers_env(tmp_path, monkeypatch):
    explicit = tmp_path / "x.xctestrun"
    explicit.write_text("x")
    monkeypatch.setenv("SWARM_RUNNER_XCTESTRUN", str(explicit))
    assert find_xctestrun("ios") == explicit


# IOSRunnerDriver


def test_ios_driver_launch_observe_and_actions(tmp_path, derived):
    driver, state, spawn, cmds = _ios_driver(tmp_path, udid=UDID)
    try:
        driver.launch()
        assert cmds.ran(f"xcrun simctl bootstatus {UDID} -b")
        install = cmds.ran(f"xcrun simctl install {UDID}")
        assert install and install[0][-1].endswith("PlantedBugs.app")
        paths = [path for _, path, _ in state.requests]
        assert paths[:3] == ["/health", "/launch", "/tree"]  # warm-up tree after launch
        assert set(driver.last_launch) == {"launch_s", "warmup_s"}

        obs = driver.observe("step-0001")
        assert obs.screenshot == tmp_path / "work" / "media" / "step-0001.png"
        assert obs.screenshot.read_bytes() == PNG
        assert obs.size == (402.0, 874.0) and obs.scale == 3.0 and obs.ts > 0
        assert obs.tree[0].children[0].identifier == "home.row.report"
        assert driver.observe(screenshot=False).screenshot is None

        driver.click(ElementQuery(identifier="home.row.report"))
        driver.tap_point(20, 30)
        driver.swipe((200, 600), (200, 200), 0.4)
        driver.keychord(["cmd+a"])
        driver.type_text(ElementQuery(label="Report"), "x")
        driver.scroll(2)
        assert driver.wait_for(ElementQuery(label="Scan"), 1).identifier == "attachments.row.scan"
        shot = driver.screenshot("plain")
        assert shot.read_bytes() == PNG
        with pytest.raises(ElementNotFoundError):
            driver.click(ElementQuery(identifier="nope"))
        with pytest.raises(UITimeoutError):
            driver.wait_for(ElementQuery(identifier="nope"), 0)
        swipes = [body for _, path, body in state.requests if path == "/swipe"]
        assert swipes[0] == {"from": [200.0, 600.0], "to": [200.0, 200.0], "duration_s": 0.4}
        assert swipes[1]["from"][1] > swipes[1]["to"][1]  # positive scroll drags up
        assert len(swipes) == 3
        meta = driver.metadata()
        assert meta.bundle_id == BUNDLE_ID and meta.version == "1.0" and meta.backend == "ios-runner"
    finally:
        driver.close()
    assert not driver.launched
    assert ("POST", "/terminate", {}) in state.requests
    driver.close()


def test_ios_driver_crash_report_and_relaunch(tmp_path, derived):
    driver, state, spawn, cmds = _ios_driver(tmp_path, udid=UDID)
    crashes = tmp_path / "crashes"
    old = _write_ips(crashes, "PlantedBugs-old", f"/Users/USER/Library/Developer/CoreSimulator/Devices/{UDID}/x", mtime=1000)
    try:
        driver.launch()
        since = time.time() - 1
        driver.click(ElementQuery(identifier="attachments.row.scan"))
        with pytest.raises(AppCrashedError):
            driver.observe()
        _write_ips(crashes, "PlantedBugs-mac", "/Users/USER/Documents/PlantedBugs.app/Contents/MacOS/PlantedBugs")
        _write_ips(crashes, "PlantedBugs-other-sim", f"/Users/USER/Library/Developer/CoreSimulator/Devices/{OTHER_UDID}/x")
        mine = _write_ips(crashes, "PlantedBugs-sim", f"/Users/USER/Library/Developer/CoreSimulator/Devices/{UDID}/data/PlantedBugs.app/PlantedBugs")
        reports = driver.crash_reports_since(since)
        assert [r.path for r in reports] == [str(mine)]
        assert reports[0].process == "PlantedBugs"
        assert "EXC_BREAKPOINT (SIGTRAP)" in reports[0].summary
        assert reports[0].extra["incident"] == "ABC"
        assert str(old) not in [r.path for r in driver.crash_reports_since(since)]

        driver.relaunch()
        assert driver.observe().tree
        assert len(spawn.calls) == 1  # the runner survived the crash
    finally:
        driver.close()


def test_relaunch_restarts_a_runner_that_stopped_serving(tmp_path, derived):
    driver, state, spawn, cmds = _ios_driver(tmp_path, udid=UDID)
    try:
        driver.launch()
        proc = spawn.procs[0]
        proc.server.stop()  # xcodebuild still alive, port closed
        proc.server = None
        driver.process.client.close()  # the fake keeps established connections alive
        driver.relaunch()
        assert driver.launched and driver.runner_restarts == 1
        assert len([c for c in spawn.calls if c[0][0] == "xcodebuild"]) == 2
        assert driver.observe().tree
    finally:
        driver.close()


def test_crash_reports_wait_for_late_report(tmp_path, derived):
    driver, state, spawn, cmds = _ios_driver(tmp_path, udid=UDID)
    driver.crash_report_wait_s = 3.0
    driver.sleep = time.sleep
    crashes = tmp_path / "crashes"
    try:
        driver.launch()
        since = time.time() - 1
        driver.click(ElementQuery(identifier="attachments.row.scan"))
        with pytest.raises(AppCrashedError):
            driver.accessibility_tree()
        timer = threading.Timer(
            0.3,
            lambda: _write_ips(crashes, "late", f"/Users/USER/Library/Developer/CoreSimulator/Devices/{UDID}/x"),
        )
        timer.start()
        assert len(driver.crash_reports_since(since)) == 1
    finally:
        driver.close()


def test_ios_driver_install_missing_app_maps_to_app_missing(tmp_path, derived):
    state = RunnerState("ios")
    state.installed = set()
    driver, state, spawn, cmds = _ios_driver(tmp_path, state=state, udid=UDID)
    with pytest.raises(AppMissingError, match="is not installed"):
        driver.launch()
    assert not driver.launched
    assert driver.process is None
    assert spawn.procs[0].signals  # runner stopped after the failed launch


def test_ios_driver_resolves_name_and_locks(tmp_path, derived):
    devices = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
                {"name": "iPhone 17", "udid": UDID, "state": "Shutdown", "isAvailable": True}
            ]
        }
    }
    cmds = FakeCommands({"xcrun simctl list devices": (0, json.dumps(devices), "")})
    driver, state, spawn, _ = _ios_driver(tmp_path, commands=cmds)
    second, *_ = _ios_driver(tmp_path / "b", commands=FakeCommands({"xcrun simctl list devices": (0, json.dumps(devices), "")}))
    try:
        driver.launch()
        assert driver.udid == UDID
        with pytest.raises(BackendUnavailable, match="already in use"):
            second.launch()
    finally:
        driver.close()
        second.close()
    assert driver.udid is None  # lock released


def test_ios_driver_pinned_env_udid(tmp_path, derived, monkeypatch):
    monkeypatch.setenv("SWARMQA_SIMULATOR_UDID", UDID)
    driver, *_ = _ios_driver(tmp_path)
    try:
        driver.launch()
        assert driver.udid == UDID
        assert driver._lock is None
    finally:
        driver.close()


def test_ios_driver_requires_darwin(tmp_path):
    driver = IOSRunnerDriver(AppTarget(path=str(tmp_path / "x.app"), platform="ios"), tmp_path, platform="linux")
    with pytest.raises(BackendUnavailable, match="Mac"):
        driver.launch()


def test_ios_driver_actions_before_launch(tmp_path):
    driver = IOSRunnerDriver(AppTarget(platform="ios"), tmp_path, platform="darwin")
    with pytest.raises(AppMissingError):
        driver.observe()


def test_ios_driver_video(tmp_path, derived):
    driver, state, spawn, cmds = _ios_driver(tmp_path, udid=UDID)
    try:
        driver.launch()
        driver.start_video()
        args, _ = spawn.calls[-1]
        assert args[:6] == ["xcrun", "simctl", "io", UDID, "recordVideo", "--force"]
        path = driver.stop_video()
        assert path == tmp_path / "work" / "media" / "session.mp4"
        assert spawn.procs[-1].signals[0] == signal.SIGINT
    finally:
        driver.close()


def test_ios_logs_since(tmp_path, derived):
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 1)) + ".123456" + time.strftime("%z", time.localtime(now))
    older = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 100)) + ".000000" + time.strftime("%z", time.localtime(now))
    lines = "\n".join(
        [
            "Filtering the log data using ...",
            json.dumps({"timestamp": older, "messageType": "Default", "eventMessage": "old"}),
            json.dumps({"timestamp": stamp, "messageType": "Error", "eventMessage": "bad thing", "subsystem": "dev.x", "processImagePath": "/a/PlantedBugs"}),
        ]
    )
    cmds = FakeCommands({f"xcrun simctl spawn {UDID} log show": (0, lines, "")})
    driver, *_ = _ios_driver(tmp_path, commands=cmds, udid=UDID)
    try:
        driver.launch()
        entries = driver.logs_since(now)
        assert [(e.level, e.message, e.process, e.subsystem) for e in entries] == [("error", "bad thing", "PlantedBugs", "dev.x")]
        call = cmds.ran(f"xcrun simctl spawn {UDID} log show")[0]
        assert call[call.index("--predicate") + 1] == 'process == "PlantedBugs"'
    finally:
        driver.close()


def test_crash_report_falls_back_to_unified_log(tmp_path, derived):
    now = time.time()
    zone = time.strftime("%z", time.localtime(now))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 2)) + ".100000" + zone
    lines = "\n".join(
        [
            json.dumps({"timestamp": stamp, "messageType": "Default", "eventMessage": "Sending UIEvent", "processImagePath": "/a/PlantedBugs"}),
            json.dumps({"timestamp": stamp, "messageType": "Default", "eventMessage": "PlantedBugs/TaskDetailView.swift:165: Fatal error: Unexpectedly found nil", "processImagePath": "/a/PlantedBugs"}),
        ]
    )
    cmds = FakeCommands({f"xcrun simctl spawn {UDID} log show": (0, lines, "")})
    driver, *_ = _ios_driver(tmp_path, commands=cmds, udid=UDID)
    try:
        driver.launch()
        since = time.time() - 1
        driver.click(ElementQuery(identifier="attachments.row.scan"))
        with pytest.raises(AppCrashedError):
            driver.observe()
        reports = driver.crash_reports_since(since)
        assert len(reports) == 1
        report = reports[0]
        assert report.extra == {"source": "unified-log"}
        assert "Fatal error" in report.summary
        assert Path(report.path).read_text().count("\n") == 1
        assert driver.crash_reports_since(since) == reports  # cached, no second file
        # No crash seen in this window: no fallback.
        assert driver.crash_reports_since(time.time() + 10) == []
    finally:
        driver.close()


def test_bounded_runner_kills_process_group():
    from swarmqa.driver.runner_client import bounded_runner

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_runner(["/bin/sh", "-c", "sleep 30 & sleep 30"], timeout=0.3)
    assert time.monotonic() - started < 10
    assert bounded_runner(["/bin/sh", "-c", "echo hi"], timeout=5).stdout == "hi\n"


def test_parse_log_ndjson_ignores_junk():
    assert parse_log_ndjson("nope\n{bad json\n[]\n", 0) == []


# MacOSRunnerDriver


def _mac_driver(tmp_path, script=None, **kwargs):
    state = RunnerState("macos")
    spawn = FakeSpawn(state, script)
    cmds = FakeCommands()
    bundle = _app(tmp_path, macos=True)
    driver = MacOSRunnerDriver(
        AppTarget(path=str(bundle / "Contents" / "MacOS" / "PlantedBugs"), platform="macos"),
        tmp_path / "work",
        runner=cmds,
        spawn=spawn,
        platform="darwin",
        crash_dir=tmp_path / "crashes",
        crash_report_wait_s=0.0,
        sleep=lambda s: time.sleep(min(s, 0.01)),
        timeouts=RunnerTimeouts(health=1.0),
        **kwargs,
    )
    return driver, state, spawn, cmds, bundle


def test_macos_driver_observe_jpeg_and_crash_filter(tmp_path, derived):
    driver, state, spawn, cmds, bundle = _mac_driver(tmp_path, port=None)
    try:
        driver.launch()
        assert driver.bundle_path == bundle
        assert any(call[0].endswith("lsregister") and call[-1] == str(bundle) for call in cmds.calls)
        assert not cmds.ran("xcrun simctl")
        obs = driver.observe("s1")
        assert obs.screenshot is not None and obs.screenshot.suffix == ".jpg"
        assert obs.screenshot.read_bytes() == JPEG
        observe_bodies = [body for _, path, body in state.requests if path == "/observe"]
        assert observe_bodies[-1]["screenshot"] == "jpeg"
        since = time.time() - 1
        _write_ips(tmp_path / "crashes", "sim", f"/Users/USER/Library/Developer/CoreSimulator/Devices/{UDID}/x")
        mac = _write_ips(tmp_path / "crashes", "mac", "/Users/USER/Documents/*/PlantedBugs.app/Contents/MacOS/PlantedBugs")
        assert [r.path for r in driver.crash_reports_since(since)] == [str(mac)]
        assert driver.metadata().backend == "macos-runner"
    finally:
        driver.close()


def test_macos_driver_automation_mode_error(tmp_path, derived):
    driver, state, spawn, cmds, bundle = _mac_driver(tmp_path, script=["automation"])
    with pytest.raises(BackendUnavailable, match="automation mode"):
        driver.launch()
    assert len(spawn.calls) == 1
    assert not driver.launched


def test_macos_driver_missing_bundle(tmp_path, derived):
    driver = MacOSRunnerDriver(AppTarget(path=str(tmp_path / "Nope.app")), tmp_path, platform="darwin", runner=FakeCommands())
    with pytest.raises(AppMissingError):
        driver.launch()


# Factory and protocol


def test_create_driver_runner_selects_platform(tmp_path):
    ios = create_driver(AppTarget(platform="ios"), tmp_path, kind="runner")
    mac = create_driver(AppTarget(platform="macos"), tmp_path, kind="runner")
    assert isinstance(ios, IOSRunnerDriver)
    assert isinstance(mac, MacOSRunnerDriver)
    assert supports_v2(ios) and supports_v2(mac)
    assert mac.screenshot_format == "jpeg" and ios.screenshot_format == "png"
