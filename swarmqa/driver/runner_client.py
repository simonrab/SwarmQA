"""Client and process control for the swarm runner (`agents/swarm-runner`).

`RunnerClient` speaks the wire protocol in `agents/swarm-runner/PROTOCOL.md`
over one keep-alive `http.client` connection, one request at a time. Bodies
go through `swarmqa.driver.runner_schema`. An error body becomes a
`RunnerError` and then the driver error the rest of swarmqa understands (see
`driver_error`); the `RunnerError` is kept as `__cause__` and its code as
`exc.code`.

`RunnerProcess` starts the runner the way `agents/swarm-runner/run.sh` does
(`xcodebuild test-without-building -xctestrun ... -only-testing
<bundle>/SwarmRunnerTests/testRunServer`), waits for `GET /health`, keeps the
log in the work dir and stops the whole process group. Host commands go
through the `runner(args, **kwargs)` seam of `swarmqa.devices.commands` and
the long-lived xcodebuild through the `spawn(args, *, env, log_path)` seam, so
tests run on Linux without Xcode.
"""

from __future__ import annotations

import glob
import http.client
import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from swarmqa.devices import commands
from swarmqa.devices.locks import HostLock, LockTimeout
from swarmqa.driver import runner_schema as schema
from swarmqa.driver.protocol import CrashReport, LogEntry, ScreenObservation
from swarmqa.driver.query import find_element
from swarmqa.errors import (
    AQAError,
    AppCrashedError,
    AppMissingError,
    BackendUnavailable,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import BuildMetadata, ElementQuery, UIElement

DERIVED_DATA_ENV = "SWARM_RUNNER_DERIVED_DATA"
XCTESTRUN_ENV = "SWARM_RUNNER_XCTESTRUN"
RESULT_DIR_ENV = "SWARM_RUNNER_RESULT_DIR"
DEFAULT_DERIVED_DATA = "~/.aqa/swarm-runner/DerivedData"
READY_MARKER = "SWARM_RUNNER_READY"

AUTOMATION_MODE_TEXT = "Timed out while enabling automation mode"
AX_LOADED_TEXT = "Timed out waiting for AX loaded notification"
SCREEN_LOCKED_TEXT = "current state: Running Background"

AUTOMATION_MODE_HELP = (
    "The macOS swarm runner could not enable UI automation mode. macOS asks for "
    "an administrator password the first time: approve that prompt on the Mac's "
    "screen and try again, or run "
    "`automationmodetool enable-automationmode-without-authentication` yourself "
    "(it asks for your password once) so later runs need no prompt."
)
SCREEN_LOCKED_HELP = (
    "The macOS swarm runner could not bring the app to the front. The Mac must be "
    "logged in with the screen unlocked while the runner drives it."
)

_SCHEMES = {
    "ios": ("SwarmRunner-iOS", "iphonesimulator", "SwarmRunnerUITests-iOS"),
    "macos": ("SwarmRunner-macOS", "macosx", "SwarmRunnerUITests-macOS"),
}
# Bundle id of the XCTest runner app on the simulator (see gen_project.py).
IOS_RUNNER_BUNDLE_ID = "dev.swarmqa.SwarmRunnerUITests.ios.xctrunner"

Runner = Callable[..., Any]
Spawn = Callable[..., Any]


# Error mapping


def driver_error(error: schema.RunnerError, *, bundle_id: str | None = None) -> AQAError:
    """Map a runner error body to the driver error callers catch."""
    code, message = error.code, error.message
    text = message or code
    exc: AQAError
    if code == "app_crashed":
        exc = AppCrashedError(text)
    elif code == "not_launched":
        exc = AppMissingError(f"no app is launched in the runner: {text}")
    elif code == "not_found":
        exc = ElementNotFoundError(text)
    elif code == "timeout":
        exc = UITimeoutError(text)
    elif code == "protocol_mismatch":
        exc = BackendUnavailable(
            f"the swarm runner speaks a different protocol version ({text}); "
            "rebuild it with agents/swarm-runner/build.sh"
        )
    elif code == "bad_request" and "not installed" in message.lower():
        target = f" {bundle_id}" if bundle_id else ""
        exc = AppMissingError(
            f"app{target} is not installed where the runner can launch it: {message}. "
            "Set app.path to the built .app so the driver installs it, or install it first."
        )
    elif code == "bad_request" and SCREEN_LOCKED_TEXT.lower() in message.lower():
        exc = BackendUnavailable(f"{SCREEN_LOCKED_HELP} ({message})")
    else:
        exc = AQAError(f"runner {code}: {message}" if message else f"runner {code}")
    exc.code = code  # type: ignore[attr-defined]
    return exc


# HTTP client


@dataclass
class RunnerTimeouts:
    """Per-call timeouts in seconds.

    `warmup` covers the first snapshot after a launch, which can take up to
    ~26 s while accessibility warms up. Steady-state `/observe` is 0.1-0.3 s
    and actions 0.5-2 s, so `step` is short by comparison.
    """

    health: float = 5.0
    launch: float = 120.0
    warmup: float = 90.0
    step: float = 30.0
    observe: float = 30.0
    screenshot: float = 30.0


class RunnerClient:
    """One keep-alive connection to a runner. Not thread-safe; never pipelines."""

    # The runner closes connections idle for 120 s; reconnect before that.
    max_idle_s = 90.0

    def __init__(
        self,
        port: int,
        *,
        host: str = "127.0.0.1",
        timeouts: RunnerTimeouts | None = None,
        bundle_id: str | None = None,
    ):
        self.host = host
        self.port = int(port)
        self.timeouts = timeouts or RunnerTimeouts()
        self.bundle_id = bundle_id
        self._conn: http.client.HTTPConnection | None = None
        self._last_used = 0.0

    # Endpoints

    def health(self, *, timeout: float | None = None) -> schema.Health:
        return schema.decode_health(self._json("GET", "/health", None, timeout or self.timeouts.health))

    def launch(
        self,
        bundle_id: str,
        *,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        terminate_existing: bool = True,
        timeout: float | None = None,
    ) -> None:
        request = schema.LaunchRequest(
            bundle_id=bundle_id,
            args=list(args or []),
            env=dict(env or {}),
            terminate_existing=terminate_existing,
        )
        self._json("POST", "/launch", schema.encode_launch(request), timeout or self.timeouts.launch, bundle_id=bundle_id)

    def terminate(self, *, timeout: float | None = None) -> None:
        self._json("POST", "/terminate", {}, timeout or self.timeouts.step)

    def tree(self, *, timeout: float | None = None) -> tuple[float, list[UIElement]]:
        return schema.decode_tree(self._json("GET", "/tree", None, timeout or self.timeouts.step))

    def observe(
        self,
        screenshot: schema.ScreenshotFormat = "png",
        *,
        jpeg_quality: float = 0.7,
        timeout: float | None = None,
    ) -> schema.ObserveResponse:
        body = schema.encode_observe(schema.ObserveRequest(screenshot=screenshot, jpeg_quality=jpeg_quality))
        return schema.decode_observe_response(self._json("POST", "/observe", body, timeout or self.timeouts.observe))

    def tap(
        self,
        *,
        point: tuple[float, float] | None = None,
        query: ElementQuery | None = None,
        timeout: float | None = None,
    ) -> None:
        body = schema.encode_tap(schema.TapRequest(point=point, query=query))
        self._json("POST", "/tap", body, timeout or self.timeouts.step)

    def type_text(self, text: str, query: ElementQuery | None = None, *, timeout: float | None = None) -> None:
        body = schema.encode_type(schema.TypeRequest(text=text, query=query))
        self._json("POST", "/type", body, timeout or self.timeouts.step)

    def swipe(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        duration_s: float = 0.3,
        *,
        timeout: float | None = None,
    ) -> None:
        body = schema.encode_swipe(schema.SwipeRequest(start=start, end=end, duration_s=duration_s))
        self._json("POST", "/swipe", body, timeout or self.timeouts.step)

    def key(self, keys: list[str], *, timeout: float | None = None) -> None:
        self._json("POST", "/key", schema.encode_key(schema.KeyRequest(keys=list(keys))), timeout or self.timeouts.step)

    def screenshot_png(self, *, timeout: float | None = None) -> bytes:
        status, _, data = self._request("GET", "/screenshot", None, timeout or self.timeouts.screenshot)
        if status >= 400:
            raise self._error(status, data)
        return data

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    # Transport

    def _json(
        self,
        method: str,
        path: str,
        body: dict | None,
        timeout: float,
        *,
        bundle_id: str | None = None,
    ) -> Any:
        status, _, data = self._request(method, path, body, timeout)
        if status >= 400:
            raise self._error(status, data, bundle_id=bundle_id)
        try:
            return json.loads(data.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise schema.RunnerProtocolError(f"{method} {path}: response is not JSON") from exc

    def _error(self, status: int, data: bytes, *, bundle_id: str | None = None) -> AQAError:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = data.decode("utf-8", "replace")
        error = schema.decode_error(parsed)
        mapped = driver_error(error, bundle_id=bundle_id or self.bundle_id)
        mapped.__cause__ = error
        return mapped

    def _request(self, method: str, path: str, body: dict | None, timeout: float) -> tuple[int, dict[str, str], bytes]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {schema.PROTOCOL_HEADER: str(schema.PROTOCOL_VERSION)}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self._conn is not None and time.monotonic() - self._last_used > self.max_idle_s:
            self.close()
        for attempt in (0, 1):
            reused = self._conn is not None
            conn = self._connection(timeout)
            try:
                conn.request(method, path, body=payload, headers=headers)
                response = conn.getresponse()
                data = response.read()
            except (socket.timeout, TimeoutError) as exc:
                # The runner is still busy with this request; a late reply would be
                # read as the answer to the next one, so drop the connection.
                self.close()
                raise UITimeoutError(f"runner did not answer {method} {path} within {timeout:g}s") from exc
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as exc:
                self.close()
                if reused and attempt == 0:
                    # The runner closed an idle keep-alive connection before reading.
                    continue
                raise BackendUnavailable(f"swarm runner on port {self.port} dropped the connection ({exc})") from exc
            except (ConnectionRefusedError, OSError, http.client.HTTPException) as exc:
                self.close()
                raise BackendUnavailable(f"swarm runner on port {self.port} is not reachable ({exc})") from exc
            self._last_used = time.monotonic()
            if (response.getheader("Connection") or "").lower() == "close":
                self.close()
            return response.status, dict(response.getheaders()), data
        raise BackendUnavailable(f"swarm runner on port {self.port} is not reachable")  # pragma: no cover

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn = self._conn
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        return conn


# Process control


class _Spawned:
    """A detached process group with its output in a log file."""

    def __init__(self, proc: subprocess.Popen, log: Any):
        self.proc = proc
        self.pid = proc.pid
        self._log = log

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)

    def send_signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.proc.send_signal(sig)

    def close(self) -> None:
        try:
            self._log.close()
        except OSError:
            pass


def default_spawn(args: list[str], *, env: dict[str, str], log_path: Path) -> _Spawned:
    """Start `args` in its own process group, stdout and stderr to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except BaseException:
        log.close()
        raise
    return _Spawned(proc, log)


def bounded_runner(
    args: list[str],
    *,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
    **_: Any,
) -> subprocess.CompletedProcess:
    """Like `subprocess.run`, but a timeout kills the whole process group.

    `xcrun simctl spawn ... log show` forks grandchildren that keep the output
    pipes open, so `subprocess.run(timeout=...)` would wait on them long after
    the deadline.
    """
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def free_port(host: str = "127.0.0.1") -> int:
    """An unused TCP port on `host`, picked by the kernel."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def derived_data_dir(explicit: str | Path | None = None) -> Path:
    raw = explicit or os.environ.get(DERIVED_DATA_ENV) or DEFAULT_DERIVED_DATA
    return Path(raw).expanduser()


def find_xctestrun(platform: str, derived_data: str | Path | None = None) -> Path | None:
    """The newest `.xctestrun` build.sh made for `platform`, as run.sh finds it."""
    explicit = (os.environ.get(XCTESTRUN_ENV) or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    scheme, sdk, _ = _SCHEMES[platform]
    pattern = str(derived_data_dir(derived_data) / "Build" / "Products" / f"{scheme}_*{sdk}*.xctestrun")
    found = [Path(item) for item in glob.glob(pattern)]
    found = [item for item in found if item.is_file()]
    if not found:
        return None
    return max(found, key=lambda item: item.stat().st_mtime)


def build_script() -> Path:
    """`agents/swarm-runner/build.sh` in the checkout this package came from."""
    return Path(__file__).resolve().parents[2] / "agents" / "swarm-runner" / "build.sh"


class RunnerStartError(BackendUnavailable):
    """The runner did not come up. `retry` is False when retrying cannot help."""

    def __init__(self, message: str, *, retry: bool = True):
        super().__init__(message)
        self.retry = retry


class RunnerProcess:
    """Start, supervise and stop one swarm runner (`xcodebuild test-without-building`).

    `platform` is `ios` (needs `udid` of a booted simulator) or `macos`.
    `port=None` asks the kernel for a free port. The runner log goes to
    `<work_dir>/runner/runner-<platform>-<port>.log` and the result bundle next
    to it. `start()` retries once, because the first start on a freshly booted
    simulator can fail with "Timed out waiting for AX loaded notification".
    """

    def __init__(
        self,
        platform: str,
        work_dir: Path,
        *,
        udid: str | None = None,
        port: int | None = None,
        derived_data: str | Path | None = None,
        auto_build: bool = True,
        ready_timeout_s: float = 300.0,
        stop_timeout_s: float = 15.0,
        build_timeout_s: float = 1800.0,
        attempts: int = 2,
        runner: Runner | None = None,
        spawn: Spawn | None = None,
        timeouts: RunnerTimeouts | None = None,
        poll_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if platform not in _SCHEMES:
            raise ValueError(f"unknown runner platform: {platform}")
        if platform == "ios" and not udid:
            raise ValueError("the iOS runner needs a simulator udid")
        self.platform = platform
        self.work_dir = Path(work_dir)
        self.udid = udid
        self.requested_port = port
        self.port: int | None = None
        self.derived_data = derived_data_dir(derived_data)
        self.auto_build = auto_build
        self.ready_timeout_s = ready_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self.build_timeout_s = build_timeout_s
        self.attempts = max(1, attempts)
        self.runner: Runner = runner if runner is not None else bounded_runner
        self.spawn: Spawn = spawn if spawn is not None else default_spawn
        self.timeouts = timeouts or RunnerTimeouts()
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock
        self.proc: Any = None
        self.client: RunnerClient | None = None
        self.log_path: Path | None = None
        self.health: schema.Health | None = None
        self.start_attempts = 0

    # Public

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> RunnerClient:
        """Start the runner and wait for `/health`. Returns the connected client."""
        if self.client is not None and self.running:
            return self.client
        xctestrun = self.ensure_built()
        last: RunnerStartError | None = None
        for attempt in range(self.attempts):
            self.start_attempts = attempt + 1
            try:
                return self._start_once(xctestrun)
            except RunnerStartError as exc:
                last = exc
                self.stop()
                if not exc.retry:
                    raise
        assert last is not None
        raise RunnerStartError(f"{last} (after {self.attempts} attempts)", retry=False) from last

    def stop(self) -> None:
        """Stop the runner's process group. Safe to call more than once."""
        if self.client is not None:
            self.client.close()
            self.client = None
        proc, self.proc = self.proc, None
        if proc is not None:
            self._terminate(proc)
            close = getattr(proc, "close", None)
            if close is not None:
                close()
        if self.platform == "ios" and self.udid and proc is not None:
            # A killed xcodebuild can leave the runner app alive on the simulator.
            commands.run(self.runner, ["xcrun", "simctl", "terminate", self.udid, IOS_RUNNER_BUNDLE_ID], timeout=30)

    def log_tail(self, limit: int = 4000) -> str:
        if self.log_path is None or not self.log_path.is_file():
            return ""
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return ""
        return data[-limit:].decode("utf-8", "replace")

    def ensure_built(self) -> Path:
        """Return the `.xctestrun`, running build.sh once if it is missing."""
        found = find_xctestrun(self.platform, self.derived_data)
        if found is not None:
            return found
        script = build_script()
        where = self.derived_data / "Build" / "Products"
        if not self.auto_build or not script.is_file():
            raise RunnerStartError(
                f"no swarm runner build for {self.platform} under {where}. "
                f"Run `agents/swarm-runner/build.sh {self.platform}` "
                f"(with {DERIVED_DATA_ENV}={self.derived_data} if you changed it).",
                retry=False,
            )
        lock = HostLock(f"swarm-runner-build-{self.platform}")
        try:
            lock.acquire(self.build_timeout_s, sleep=self.sleep, clock=self.clock)
        except LockTimeout as exc:
            raise RunnerStartError(f"another process is still building the swarm runner: {exc}", retry=False) from exc
        try:
            found = find_xctestrun(self.platform, self.derived_data)
            if found is not None:
                return found
            env = dict(os.environ)
            env[DERIVED_DATA_ENV] = str(self.derived_data)
            result = commands.run(self.runner, [str(script), self.platform], env=env, timeout=self.build_timeout_s)
        finally:
            lock.release()
        found = find_xctestrun(self.platform, self.derived_data)
        if not result.ok or found is None:
            raise RunnerStartError(
                f"building the swarm runner failed (`{script} {self.platform}`, exit {result.returncode}): "
                f"{result.detail()[-1500:]}",
                retry=False,
            )
        return found

    def command(self, xctestrun: Path, port: int, result_bundle: Path) -> list[str]:
        """The xcodebuild invocation, the same as run.sh."""
        _, _, bundle = _SCHEMES[self.platform]
        if self.platform == "ios":
            destination = f"platform=iOS Simulator,id={self.udid}"
        else:
            destination = f"platform=macOS,arch={os.uname().machine}"
        return [
            "xcodebuild",
            "test-without-building",
            "-xctestrun",
            str(xctestrun),
            "-destination",
            destination,
            "-only-testing",
            f"{bundle}/SwarmRunnerTests/testRunServer",
            "-resultBundlePath",
            str(result_bundle),
            "-test-timeouts-enabled",
            "NO",
            "-collect-test-diagnostics",
            "never",
        ]

    # Internals

    def _start_once(self, xctestrun: Path) -> RunnerClient:
        port = self.requested_port or free_port()
        self.port = port
        run_dir = self.work_dir / "runner"
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = run_dir / f"runner-{self.platform}-{port}-{self.start_attempts}.log"
        result_root = Path(os.environ.get(RESULT_DIR_ENV) or run_dir).expanduser()
        result_root.mkdir(parents=True, exist_ok=True)
        result_bundle = result_root / f"{self.platform}-{port}-{stamp}-{os.getpid()}-{self.start_attempts}.xcresult"
        env = dict(os.environ)
        env["TEST_RUNNER_" + schema.PORT_ENV] = str(port)
        args = self.command(xctestrun, port, result_bundle)
        try:
            self.proc = self.spawn(args, env=env, log_path=self.log_path)
        except FileNotFoundError as exc:
            raise RunnerStartError(
                "xcodebuild is not available. Install Xcode, or use the fake driver.", retry=False
            ) from exc
        client = RunnerClient(port, timeouts=self.timeouts)
        deadline = self.clock() + self.ready_timeout_s
        while True:
            self._check_log()
            code = self.proc.poll()
            if code is not None:
                self._check_log()
                raise RunnerStartError(
                    f"swarm runner ({self.platform}) exited with {code} before it was ready. "
                    f"Log: {self.log_path}\n{self._tail_lines()}"
                )
            try:
                self.health = client.health(timeout=min(self.timeouts.health, 5.0))
            except (BackendUnavailable, UITimeoutError, schema.RunnerProtocolError, AQAError):
                client.close()
            else:
                if self.health.protocol_version != schema.PROTOCOL_VERSION:
                    raise RunnerStartError(
                        f"swarm runner speaks protocol {self.health.protocol_version}, "
                        f"expected {schema.PROTOCOL_VERSION}; rebuild it with build.sh",
                        retry=False,
                    )
                self.client = client
                return client
            if self.clock() >= deadline:
                self._check_log()
                raise RunnerStartError(
                    f"swarm runner ({self.platform}) did not answer /health on port {port} "
                    f"within {self.ready_timeout_s:g}s. Log: {self.log_path}\n{self._tail_lines()}"
                )
            self.sleep(self.poll_s)

    def _check_log(self) -> None:
        text = self.log_tail(20000)
        if AUTOMATION_MODE_TEXT in text:
            raise RunnerStartError(f"{AUTOMATION_MODE_HELP} Log: {self.log_path}", retry=False)
        if AX_LOADED_TEXT in text and self.proc is not None and self.proc.poll() is not None:
            raise RunnerStartError(f"swarm runner start failed: {AX_LOADED_TEXT}. Log: {self.log_path}")

    def _tail_lines(self, count: int = 15) -> str:
        lines = [line for line in self.log_tail().splitlines() if line.strip()]
        return "\n".join(lines[-count:])

    def _terminate(self, proc: Any) -> None:
        if proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, self.stop_timeout_s), (signal.SIGKILL, 5.0)):
            try:
                proc.send_signal(sig)
            except OSError:
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                return


# Shared driver behaviour for IOSRunnerDriver and MacOSRunnerDriver

_KEY_ALIASES = {
    "command": "cmd",
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    "enter": "return",
    "esc": "escape",
    "backspace": "delete",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}
_LOG_LEVELS = {"debug": "debug", "info": "info", "default": "notice", "error": "error", "fault": "fault"}
DIAGNOSTIC_REPORTS = "~/Library/Logs/DiagnosticReports"


def normalise_keys(keys: list[str]) -> list[str]:
    """`["cmd+s"]` or `["Command", "S"]` to the runner's names (`["cmd", "s"]`)."""
    out: list[str] = []
    for key in keys:
        pieces = [key] if key == "+" else [piece for piece in key.split("+") if piece != ""]
        for piece in pieces:
            name = piece.strip()
            if not name:
                continue
            low = name.lower()
            out.append(_KEY_ALIASES.get(low, low if len(name) > 1 else name))
    return out


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")
    return (cleaned or "screenshot")[:80]


def read_crash_report(path: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Split an `.ips` file into its header line and JSON body. None if unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head, _, rest = text.partition("\n")
    try:
        header = json.loads(head)
    except json.JSONDecodeError:
        return None
    if not isinstance(header, dict):
        return None
    try:
        body = json.loads(rest) if rest.strip() else {}
    except json.JSONDecodeError:
        body = {}
    return header, body if isinstance(body, dict) else {}


def crash_summary(header: dict[str, Any], body: dict[str, Any]) -> str:
    exception = body.get("exception") if isinstance(body.get("exception"), dict) else {}
    termination = body.get("termination") if isinstance(body.get("termination"), dict) else {}
    parts = []
    kind = exception.get("type")
    if kind:
        sig = exception.get("signal")
        parts.append(f"{kind} ({sig})" if sig else str(kind))
    indicator = termination.get("indicator")
    if indicator:
        parts.append(str(indicator))
    name = header.get("app_name") or body.get("procName") or "app"
    return f"{name} crashed: {', '.join(parts)}" if parts else f"{name} crashed"


def parse_log_ndjson(text: str, ts: float) -> list[LogEntry]:
    """`log show --style ndjson` lines at or after `ts` as LogEntry, oldest first."""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        stamp = str(item.get("timestamp") or "")
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
        except ValueError:
            continue
        if when < ts:
            continue
        level = _LOG_LEVELS.get(str(item.get("messageType") or "default").lower(), "notice")
        entries.append(
            LogEntry(
                ts=when,
                level=level,  # type: ignore[arg-type]
                message=str(item.get("eventMessage") or ""),
                subsystem=str(item.get("subsystem") or ""),
                process=os.path.basename(str(item.get("processImagePath") or "")),
            )
        )
    entries.sort(key=lambda entry: entry.ts)
    return entries


def log_start_arg(ts: float) -> str:
    """`log show --start` takes local wall-clock time."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(0.0, ts)))


def read_info_plist(bundle: Path) -> dict[str, Any]:
    for candidate in (bundle / "Info.plist", bundle / "Contents" / "Info.plist"):
        if candidate.is_file():
            try:
                with candidate.open("rb") as handle:
                    data = plistlib.load(handle)
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


_FATAL_TEXT = re.compile(
    r"Fatal error|fatalError|Terminating app due to uncaught exception|EXC_[A-Z_]+|SIGABRT|SIGSEGV|SIGTRAP"
)


class RunnerDriverBase:
    """AppDriver v1 and v2 over a swarm runner. Subclasses supply the platform parts.

    Subclasses implement `_ensure_host`, `_prepare` (resolve the bundle and the
    device, install), `_make_process`, and optionally `_crash_matches`,
    `_log_command`, `_release`, `start_video` and `stop_video`.
    """

    backend = "runner"
    default_screenshot_format: schema.ScreenshotFormat = "png"
    scroll_distance = 180.0

    def __init__(
        self,
        target: Any,
        work_dir: Path,
        video_mode: str = "always",
        *,
        runner: Runner | None = None,
        spawn: Spawn | None = None,
        platform: str | None = None,
        port: int | None = None,
        derived_data: str | Path | None = None,
        timeouts: RunnerTimeouts | None = None,
        screenshot_format: schema.ScreenshotFormat | None = None,
        jpeg_quality: float = 0.7,
        crash_dir: str | Path | None = None,
        crash_report_wait_s: float = 10.0,
        ready_timeout_s: float = 300.0,
        auto_build: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.target = target
        self.work_dir = Path(work_dir)
        self.video_mode = video_mode
        self.runner: Runner = runner if runner is not None else bounded_runner
        self.spawn: Spawn = spawn if spawn is not None else default_spawn
        self.platform = sys.platform if platform is None else platform
        self.port = port
        self.derived_data = derived_data
        self.timeouts = timeouts or RunnerTimeouts()
        self.screenshot_format: schema.ScreenshotFormat = screenshot_format or self.default_screenshot_format
        self.jpeg_quality = jpeg_quality
        self.crash_dir = Path(crash_dir or DIAGNOSTIC_REPORTS).expanduser()
        self.crash_report_wait_s = crash_report_wait_s
        self.ready_timeout_s = ready_timeout_s
        self.auto_build = auto_build
        self.sleep = sleep
        self.launched = False
        self.bundle_id: str | None = None
        self.bundle_path: Path | None = None
        self.process_name: str | None = None
        self.version: str | None = None
        self.process: RunnerProcess | None = None
        self.launch_ts: float | None = None
        self.last_launch: dict[str, float] = {}
        self.screen_size: tuple[float, float] | None = None
        self._crash_seen_at: float | None = None
        self._crash_waited = False
        self._log_crashes: dict[float, CrashReport] = {}
        self.log_timeout_s = 30.0
        self.runner_restarts = 0
        self._names = 0
        self._video_proc: Any = None
        self._video_path: Path | None = None
        self._video_on = False
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # Lifecycle

    def launch(self) -> None:
        if self.launched:
            return
        self._ensure_host()
        try:
            self._prepare()
            client = self._ensure_runner()
            self._launch_app(client)
        except Exception:
            self._shutdown()
            raise
        self.launched = True

    def relaunch(self) -> None:
        process = self.process
        if process is None or process.client is None or not process.running:
            self._shutdown()
            self.launched = False
            self.launch()
            return
        self.launched = False
        try:
            self._launch_app(process.client)
        except BackendUnavailable:
            # The runner stopped serving (seen once on a loaded 8 GB Mac right
            # after an app crash: xcodebuild alive, port closed). Start a new one.
            self.runner_restarts += 1
            self._shutdown()
            self.launch()
            return
        self.launched = True

    def close(self) -> None:
        if self._video_on:
            try:
                self.stop_video()
            except Exception:
                pass
        process = self.process
        if process is not None and process.client is not None and process.running:
            try:
                process.client.terminate(timeout=15)
            except Exception:
                pass
        self._shutdown()
        self.launched = False

    def metadata(self) -> BuildMetadata:
        return BuildMetadata(
            path=str(self.bundle_path) if self.bundle_path else getattr(self.target, "path", None),
            bundle_id=self.bundle_id or getattr(self.target, "bundle_id", None),
            version=self.version,
            backend=self.backend,
        )

    # v1 actions

    def accessibility_tree(self) -> list[UIElement]:
        client = self._client()
        _, elements = self._call(lambda: client.tree())
        return elements

    def click(self, target: ElementQuery) -> None:
        client = self._client()
        self._call(lambda: client.tap(query=target))

    def type_text(self, target: ElementQuery, text: str) -> None:
        client = self._client()
        self._call(lambda: client.type_text(text, target))

    def keychord(self, keys: list[str]) -> None:
        names = normalise_keys(keys)
        if not names:
            raise ElementNotFoundError("keychord has no key")
        client = self._client()
        self._call(lambda: client.key(names))

    def scroll(self, delta: int, target: ElementQuery | None = None) -> None:
        if delta == 0:
            return
        x, y = self._scroll_origin(target)
        half = self.scroll_distance / 2
        for _ in range(min(abs(int(delta)), 5)):
            if delta > 0:  # the finger moves up, so content below comes into view
                self.swipe((x, y + half), (x, y - half))
            else:
                self.swipe((x, y - half), (x, y + half))

    def select_menu(self, path: list[str]) -> None:
        if not path:
            raise ElementNotFoundError("menu path is empty")
        for label in path:
            self.click(ElementQuery(label=label))

    def wait_for(self, target: ElementQuery, timeout_s: float) -> UIElement:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                return find_element(self.accessibility_tree(), target)
            except ElementNotFoundError as exc:
                if time.monotonic() >= deadline:
                    raise UITimeoutError(f"timed out after {timeout_s}s waiting for {exc}") from exc
                self.sleep(0.25)

    def screenshot(self, name: str) -> Path:
        client = self._client()
        data = self._call(lambda: client.screenshot_png())
        path = self._media_dir() / f"{safe_name(name)}.png"
        path.write_bytes(data)
        return path

    # v2

    def observe(self, name: str | None = None, *, screenshot: bool = True) -> ScreenObservation:
        client = self._client()
        fmt: schema.ScreenshotFormat = self.screenshot_format if screenshot else "none"
        response = self._call(lambda: client.observe(fmt, jpeg_quality=self.jpeg_quality))
        path = None
        if response.screenshot is not None:
            self._names += 1
            stem = safe_name(name) if name else f"observe-{self._names:04d}"
            suffix = ".jpg" if response.screenshot.format == "jpeg" else ".png"
            path = self._media_dir() / f"{stem}{suffix}"
            path.write_bytes(response.screenshot.data)
        if response.size is not None:
            self.screen_size = response.size
        return ScreenObservation(
            tree=response.elements,
            ts=response.ts,
            screenshot=path,
            size=response.size,
            scale=response.scale,
        )

    def tap_point(self, x: float, y: float) -> None:
        client = self._client()
        self._call(lambda: client.tap(point=(float(x), float(y))))

    def swipe(self, start: tuple[float, float], end: tuple[float, float], duration_s: float = 0.3) -> None:
        client = self._client()
        self._call(lambda: client.swipe(start, end, duration_s))

    def logs_since(self, ts: float) -> list[LogEntry]:
        if not self.process_name:
            return []
        command = self._log_command(ts)
        if not command:
            return []
        result = commands.run(self.runner, command, timeout=self.log_timeout_s)
        if not result.ok:
            return []
        return parse_log_ndjson(result.stdout, ts)

    def crash_reports_since(self, ts: float) -> list[CrashReport]:
        """`.ips` reports for this app at or after `ts`.

        ReportCrash writes the `.ips` a few seconds after the process dies, so
        when this session saw the crash the first call waits up to
        `crash_report_wait_s`. An app launched by XCUITest that dies of a Swift
        runtime trap often gets no `.ips` on disk at all (measured on iOS 26.5);
        then the app's fatal log lines are saved to `<work_dir>/crashes/` and
        returned as a report with `extra["source"] = "unified-log"`.
        """
        seen = self._crash_seen_at is not None and self._crash_seen_at >= ts
        wait = 0.0
        if seen and not self._crash_waited:
            wait = self.crash_report_wait_s
            self._crash_waited = True
        deadline = time.monotonic() + wait
        while True:
            reports = self._scan_crash_reports(ts)
            if reports or time.monotonic() >= deadline:
                break
            self.sleep(0.5)
        if reports or not seen:
            return reports
        fallback = self._crash_from_log(ts)
        return [fallback] if fallback is not None else []

    # Internals

    def _ensure_runner(self) -> RunnerClient:
        if self.process is None:
            self.process = self._make_process()
        client = self.process.start()
        client.bundle_id = self.bundle_id
        return client

    def _launch_app(self, client: RunnerClient) -> None:
        bundle_id = self.bundle_id or ""
        started = time.monotonic()
        self.launch_ts = time.time()
        self._crash_seen_at = None
        self._crash_waited = False
        args = [str(item) for item in (getattr(self.target, "launch_args", None) or [])]
        env = {str(k): str(v) for k, v in (getattr(self.target, "env", None) or {}).items()}
        self._call(lambda: client.launch(bundle_id, args=args, env=env, terminate_existing=True))
        launched = time.monotonic()
        # The first snapshot after a launch waits for accessibility to warm up
        # (up to ~26 s measured); later ones take 0.1-0.3 s.
        try:
            self._call(lambda: client.tree(timeout=self.timeouts.warmup))
        except UITimeoutError:
            pass
        self.last_launch = {"launch_s": launched - started, "warmup_s": time.monotonic() - launched}

    def _client(self) -> RunnerClient:
        process = self.process
        if not self.launched or process is None or process.client is None:
            raise AppMissingError("app is not launched")
        if not process.running:
            raise BackendUnavailable(f"the swarm runner exited. Log: {process.log_path}\n{process._tail_lines()}")
        return process.client

    def _call(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except AppCrashedError:
            if self._crash_seen_at is None:
                self._crash_seen_at = time.time()
            raise
        except BackendUnavailable:
            process = self.process
            if process is not None and process.proc is not None and not process.running:
                raise BackendUnavailable(
                    f"the swarm runner exited. Log: {process.log_path}\n{process._tail_lines()}"
                ) from None
            raise

    def _scroll_origin(self, target: ElementQuery | None) -> tuple[float, float]:
        if target is not None:
            element = find_element(self.accessibility_tree(), target)
            if element.frame is not None:
                x, y, w, h = element.frame
                return (x + w / 2, y + h / 2)
        if self.screen_size is not None:
            return (self.screen_size[0] / 2, self.screen_size[1] / 2)
        for element in self.accessibility_tree():
            if element.frame is not None:
                x, y, w, h = element.frame
                return (x + w / 2, y + h / 2)
        return (195.0, 422.0)

    def _scan_crash_reports(self, ts: float) -> list[CrashReport]:
        if not self.crash_dir.is_dir():
            return []
        found = []
        for path in self.crash_dir.glob("*.ips"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < ts:
                continue
            parsed = read_crash_report(path)
            if parsed is None:
                continue
            header, body = parsed
            names = {str(header.get("app_name") or ""), str(body.get("procName") or "")}
            same_app = bool(self.bundle_id and header.get("bundleID") == self.bundle_id) or bool(
                self.process_name and self.process_name in names
            )
            if not same_app or not self._crash_matches(header, body):
                continue
            extra = {
                "incident": header.get("incident_id") or body.get("incident"),
                "bundle_id": header.get("bundleID"),
                "os_version": header.get("os_version"),
                "capture_time": body.get("captureTime"),
            }
            found.append(
                CrashReport(
                    ts=mtime,
                    process=str(body.get("procName") or header.get("app_name") or self.process_name or ""),
                    path=str(path),
                    summary=crash_summary(header, body),
                    extra={key: str(value) for key, value in extra.items() if value},
                )
            )
        found.sort(key=lambda report: report.ts)
        return found

    def _crash_from_log(self, ts: float) -> CrashReport | None:
        crash_at = self._crash_seen_at or ts
        cached = self._log_crashes.get(crash_at)
        if cached is not None:
            return cached
        start = max(ts, self.launch_ts or ts) - 1.0
        entries = self.logs_since(start)
        fatal = [entry for entry in entries if _FATAL_TEXT.search(entry.message)]
        fatal = fatal or [entry for entry in entries if entry.level == "fault"]
        if not fatal:
            return None
        folder = self.work_dir / "crashes"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"crash-{len(self._log_crashes) + 1}.log"
        lines = [
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.ts))} {e.level} {e.process}: {e.message}"
            for e in fatal
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        main = next((e for e in fatal if _FATAL_TEXT.search(e.message)), fatal[-1])
        report = CrashReport(
            ts=main.ts,
            process=self.process_name or "",
            path=str(path),
            summary=main.message.strip().splitlines()[0][:300] if main.message.strip() else "app crashed",
            extra={"source": "unified-log"},
        )
        self._log_crashes[crash_at] = report
        return report

    def _media_dir(self) -> Path:
        media = self.work_dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        return media

    def _shutdown(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            process.stop()
        self._release()

    def _start_video_process(self, args: list[str], path: Path) -> None:
        if self._video_on:
            return
        try:
            proc = self.spawn(args, env=dict(os.environ), log_path=self._media_dir() / "video.log")
        except OSError:
            proc = None
        if proc is None or proc.poll() is not None:
            note = self._media_dir() / "session.txt"
            note.write_text(f"video unavailable: {args[0]} failed to start\n", encoding="utf-8")
            self._video_proc, self._video_path = None, note
        else:
            self._video_proc, self._video_path = proc, path
        self._video_on = True

    def _stop_video_process(self) -> Path | None:
        proc, path = self._video_proc, self._video_path
        self._video_proc = None
        self._video_on = False
        if proc is not None:
            # recordVideo and screencapture finish the movie on SIGINT; SIGKILL drops it.
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.send_signal(signal.SIGKILL)
                except Exception:
                    pass
            close = getattr(proc, "close", None)
            if close is not None:
                close()
        return path

    # Subclass hooks

    def _ensure_host(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _prepare(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _make_process(self) -> RunnerProcess:  # pragma: no cover - abstract
        raise NotImplementedError

    def _crash_matches(self, header: dict[str, Any], body: dict[str, Any]) -> bool:
        return True

    def _log_command(self, ts: float) -> list[str] | None:
        return None

    def _release(self) -> None:
        return None

    def start_video(self) -> None:
        return None

    def stop_video(self) -> Path | None:
        return None
