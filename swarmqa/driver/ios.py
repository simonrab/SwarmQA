"""iOS Simulator driver.

One instance drives one Simulator session for one worker. Lifecycle commands
go through ``xcrun simctl``. Taps, typing, and the accessibility tree go
through ``idb``. Subprocess calls go through ``self.runner`` so Linux tests
can supply Simulator output without Xcode.

Importing this module does not require macOS, Xcode, or a booted Simulator.
``launch`` on any other platform raises ``BackendUnavailable``.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from swarmqa.devices.locks import HostLock, simulator_lock_name, try_lock
from swarmqa.driver.query import find_element
from swarmqa.errors import (
    AQAError,
    AppCrashedError,
    AppMissingError,
    BackendUnavailable,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import AppTarget, BuildMetadata, ElementQuery, UIElement

Runner = Callable[..., Any]

_NON_DARWIN = (
    "IOSSimulatorDriver cannot launch apps on this host. "
    "Use the fake driver or a Mac with Xcode."
)
_NO_XCRUN = (
    "xcrun is not available. Install Xcode and the iOS Simulator, or use the fake driver."
)
_NO_IDB = (
    "idb is required to read and drive the iOS Simulator accessibility tree. "
    "simctl ui only changes appearance, contrast, and content size. "
    "Install idb with `brew tap facebook/fb` and `brew install facebook/fb/idb` "
    "(client and companion; needs Python 3.10+), or use the fake driver."
)
_DEFAULT_DEVICE = "iPhone 17"
_UDID_ENV = "SWARMQA_SIMULATOR_UDID"

_UDID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_APP_PATH = re.compile(r"((?:~|/|\.{1,2}/)[^\s\"']+?\.app)\b")
_FRAME_TEXT = re.compile(
    r"\{\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\}"
    r"\s*,\s*\{\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\}"
)

_ROLE_MAP = {
    "application": "window",
    "window": "window",
    "button": "button",
    "textfield": "textfield",
    "searchfield": "textfield",
    "securetextfield": "textfield",
    "textarea": "textarea",
    "textview": "textarea",
    "statictext": "text",
    "text": "text",
    "image": "image",
    "switch": "checkbox",
    "toggle": "checkbox",
    "checkbox": "checkbox",
    "slider": "slider",
    "link": "link",
    "cell": "cell",
    "table": "table",
    "collectionview": "list",
    "scrollview": "scrollarea",
    "navigationbar": "group",
    "tabbar": "group",
    "toolbar": "group",
    "menu": "menu",
    "menuitem": "menuitem",
    "other": "group",
    "group": "group",
}

_IOS_KEYS = {
    "return": "40",
    "enter": "40",
    "esc": "41",
    "escape": "41",
    "delete": "42",
    "backspace": "42",
    "tab": "43",
    "space": "44",
    "right": "79",
    "left": "80",
    "down": "81",
    "up": "82",
}
_MODIFIERS = {"cmd", "command", "shift", "option", "alt", "opt", "ctrl", "control"}


def _default_runner(
    args: list[str],
    *,
    background: bool = False,
    input: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Any:
    """Run a command, or spawn it when ``background`` is set."""
    if background:
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    return subprocess.run(
        args,
        input=input,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def _stdout(result: Any) -> str:
    data = getattr(result, "stdout", "") or ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _stderr(result: Any) -> str:
    data = getattr(result, "stderr", "") or ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _canonical_role(role: str) -> str:
    text = (role or "").strip()
    key = text.lower().replace(" ", "").replace("_", "")
    if key.startswith("xcuielementtype"):
        key = key[len("xcuielementtype") :]
    return _ROLE_MAP.get(key, key or "unknown")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _frame_from(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        x = _number(value.get("x", value.get("X")))
        y = _number(value.get("y", value.get("Y")))
        width = _number(value.get("width", value.get("Width")))
        height = _number(value.get("height", value.get("Height")))
        if None not in (x, y, width, height):
            return (x, y, width, height)  # type: ignore[return-value]
    if isinstance(value, str):
        match = _FRAME_TEXT.search(value)
        if match:
            return tuple(float(part) for part in match.groups())  # type: ignore[return-value]
    return None


def _label_of(node: dict[str, Any]) -> str:
    for key in ("AXLabel", "label", "title", "AXTitle"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _text_of(node: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = node.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _node_element(node: dict[str, Any]) -> UIElement:
    role = _canonical_role(str(node.get("type") or node.get("role") or node.get("AXRole") or ""))
    children_raw = node.get("children") or []
    children = [_node_element(child) for child in children_raw if isinstance(child, dict)]
    enabled = node.get("enabled", True)
    return UIElement(
        role=role,
        label=_label_of(node),
        identifier=_text_of(node, "AXUniqueId", "identifier", "id"),
        value=_text_of(node, "AXValue", "value"),
        enabled=bool(enabled),
        frame=_frame_from(node.get("frame") or node.get("AXFrame")),
        children=children,
    )


def parse_idb_tree(payload: str | bytes | Any) -> list[UIElement]:
    """Parse ``idb ui describe-all`` JSON into ``UIElement`` nodes."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        text = payload.strip()
        start = min((index for index in (text.find("["), text.find("{")) if index >= 0), default=-1)
        if start < 0:
            raise AQAError("idb did not return an accessibility tree")
        try:
            data = json.loads(text[start:])
        except json.JSONDecodeError as exc:
            raise AQAError("idb did not return an accessibility tree") from exc
    else:
        data = payload
    nodes: list[Any]
    if isinstance(data, list):
        nodes = data
    elif isinstance(data, dict):
        nested = data.get("children") or data.get("tree") or data.get("elements")
        if isinstance(nested, list):
            nodes = nested
        else:
            nodes = [data]
    else:
        raise AQAError("idb did not return an accessibility tree")
    return [_node_element(node) for node in nodes if isinstance(node, dict)]


def select_simulator(document: Any, name: str) -> str:
    """Pick a UDID from ``simctl list devices -j``.

    Prefer a booted device with that name, then the newest runtime that has
    an available match.
    """
    if not isinstance(document, dict):
        raise BackendUnavailable("simctl list did not return a device list")
    devices = document.get("devices")
    if not isinstance(devices, dict):
        raise BackendUnavailable("simctl list did not return a device list")
    wanted = name.strip().lower()
    matches: list[tuple[tuple[int, ...], bool, str]] = []
    for runtime, entries in devices.items():
        if not isinstance(entries, list):
            continue
        version = tuple(int(part) for part in re.findall(r"\d+", str(runtime)))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("isAvailable") is False:
                continue
            device_name = str(entry.get("name") or "").strip().lower()
            udid = str(entry.get("udid") or "").strip()
            if not udid or device_name != wanted:
                continue
            booted = str(entry.get("state") or "").lower() == "booted"
            matches.append((version, booted, udid))
    if not matches:
        raise BackendUnavailable(
            f"No available iOS Simulator named {name!r}. "
            "Run `xcrun simctl list devices available` and set app.simulator "
            "to a device name or UDID."
        )
    matches.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return matches[0][2]


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")
    return (cleaned or "screenshot")[:80]


def _discover_app_path(text: str) -> str | None:
    match = _APP_PATH.search(text or "")
    if not match:
        return None
    return os.path.expanduser(match.group(1))


def _read_plist(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _plist_str(info: dict[str, Any], key: str) -> str | None:
    value = info.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_udid(value: str) -> bool:
    return bool(_UDID.match(value.strip()))


def _coord(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _center(frame: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, width, height = frame
    return (x + width / 2, y + height / 2)


class IOSSimulatorDriver:
    """Drive an iOS ``.app`` in the Simulator through simctl and idb.

    ``runner`` replaces every subprocess call. Tests pass a callable with the
    same shape as :func:`_default_runner`. ``platform`` defaults to
    ``sys.platform``; Linux tests that exercise launch pass ``platform="darwin"``.
    ``udid`` pins one Simulator and skips ``app.simulators``.
    """

    def __init__(
        self,
        target: AppTarget,
        work_dir: Path,
        video_mode: str = "always",
        *,
        runner: Runner | None = None,
        platform: str | None = None,
        udid: str | None = None,
    ):
        self.target = target
        self.work_dir = Path(work_dir)
        self.video_mode = video_mode
        self.runner: Runner = runner if runner is not None else _default_runner
        self.platform = sys.platform if platform is None else platform
        self._udid_override = (udid or "").strip() or None
        self.launched = False
        self.udid: str | None = None
        self._bundle_path: Path | None = None
        self._bundle_id: str | None = None
        self._pid: int | None = None
        self._video_on = False
        self._video_proc: Any = None
        self._video_path: Path | None = None
        self._lock: HostLock | None = None
        self._command_timeout = 60.0
        self._poll_s = 0.05
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def launch(self) -> None:
        if self.launched:
            return
        self._ensure_host()
        try:
            bundle = self._prepare_bundle()
            token = self._claim_simulator()
            udid = self._resolve_udid(token)
            self._boot(udid)
            self._install(udid, bundle)
            self._start(udid)
        except Exception:
            self._release_lock()
            self.udid = None
            raise
        self.launched = True

    def relaunch(self) -> None:
        if not self.launched:
            self.launch()
            return
        udid = self.udid
        bundle_id = self._bundle_id
        self._terminate_app()
        self.launched = False
        self._pid = None
        if not udid or not bundle_id:
            self.launch()
            return
        try:
            self._start(udid)
        except Exception:
            self.launched = False
            raise
        self.launched = True

    def close(self) -> None:
        if self._video_on:
            self.stop_video()
        self._terminate_app()
        self._release_lock()
        self.launched = False
        self._pid = None

    def accessibility_tree(self) -> list[UIElement]:
        self._require_launched()
        self._require_idb()
        result = self._idb("ui", "describe-all")
        self._check_action(result, "accessibility tree")
        return parse_idb_tree(_stdout(result))

    def click(self, target: ElementQuery) -> None:
        element = self._resolve(target)
        if element.frame is None:
            raise ElementNotFoundError(f"{element.label or element.role} has no frame")
        x, y = _center(element.frame)
        self._idb_ok("tap", "ui", "tap", _coord(x), _coord(y))

    def type_text(self, target: ElementQuery, text: str) -> None:
        self.click(target)
        self._idb_ok("type", "ui", "text", text)

    def keychord(self, keys: list[str]) -> None:
        self._require_launched()
        parts: list[str] = []
        for key in keys:
            parts.extend(piece.strip() for piece in key.split("+") if piece.strip())
        send = [part for part in parts if part.lower() not in _MODIFIERS]
        if not send:
            raise ElementNotFoundError("iOS Simulator keychord has no key")
        for key in send:
            code = _IOS_KEYS.get(key.lower())
            if code is None:
                raise ElementNotFoundError(f"unsupported iOS key: {key}")
            self._idb_ok("keychord", "ui", "key", code)

    def scroll(self, delta: int, target: ElementQuery | None = None) -> None:
        self._require_launched()
        if delta == 0:
            return
        origin = self._scroll_origin(target)
        x, y = origin
        distance = 180
        steps = min(abs(int(delta)), 5)
        for _ in range(steps):
            if delta > 0:
                start_y, end_y = y + distance / 2, y - distance / 2
            else:
                start_y, end_y = y - distance / 2, y + distance / 2
            self._idb_ok(
                "scroll",
                "ui",
                "swipe",
                _coord(x),
                _coord(start_y),
                _coord(x),
                _coord(end_y),
            )

    def select_menu(self, path: list[str]) -> None:
        self._require_launched()
        if not path:
            raise ElementNotFoundError("menu path is empty")
        for label in path:
            self.click(ElementQuery(label=label))

    def wait_for(self, target: ElementQuery, timeout_s: float) -> UIElement:
        self._require_launched()
        deadline = time.monotonic() + max(timeout_s, 0)
        while True:
            try:
                return find_element(self.accessibility_tree(), target)
            except ElementNotFoundError as exc:
                if time.monotonic() >= deadline:
                    raise UITimeoutError(f"timed out after {timeout_s}s waiting for {exc}") from exc
                time.sleep(self._poll_s)

    def screenshot(self, name: str) -> Path:
        self._require_launched()
        path = self._media_dir() / f"{_safe_name(name)}.png"
        result = self._simctl("io", self.udid or "", "screenshot", str(path))
        self._check_action(result, "screenshot")
        if not path.is_file():
            raise AQAError(f"screenshot was not written: {path}")
        return path

    def start_video(self) -> None:
        if self._video_on:
            return
        self._require_launched()
        path = self._media_dir() / "session.mp4"
        proc = self._run(
            ["xcrun", "simctl", "io", self.udid or "", "recordVideo", str(path)],
            background=True,
        )
        code = proc.poll() if hasattr(proc, "poll") else getattr(proc, "returncode", None)
        if code is not None:
            note = self._media_dir() / "session.txt"
            note.write_text("video unavailable: simctl recordVideo failed\n", encoding="utf-8")
            self._video_path = note
            self._video_proc = None
            self._video_on = True
            return
        self._video_proc = proc
        self._video_path = path
        self._video_on = True

    def stop_video(self) -> Path | None:
        path = self._video_path
        proc = self._video_proc
        self._video_on = False
        self._video_proc = None
        if proc is not None:
            self._stop_process(proc)
        return path

    def metadata(self) -> BuildMetadata:
        bundle = self._bundle_path
        if bundle is None:
            configured = self._configured_path()
            bundle = Path(configured) if configured else None
        info = _read_plist(self._plist_path(bundle)) if bundle else {}
        bundle_id = _plist_str(info, "CFBundleIdentifier") or self.target.bundle_id
        version = _plist_str(info, "CFBundleShortVersionString")
        return BuildMetadata(
            path=str(bundle) if bundle is not None else self.target.path,
            bundle_id=bundle_id,
            version=version,
            backend="ios",
        )

    def _ensure_host(self) -> None:
        if self.platform != "darwin":
            raise BackendUnavailable(_NON_DARWIN)
        if self._tool_path("xcrun") is None:
            raise BackendUnavailable(_NO_XCRUN)

    def _require_idb(self) -> None:
        if self._tool_path("idb") is None:
            raise BackendUnavailable(_NO_IDB)

    def _require_launched(self) -> None:
        if not self.launched:
            raise AppMissingError("app is not launched")
        self._assert_running()

    def _assert_running(self) -> None:
        if not self.udid or self._pid is None:
            return
        result = self._simctl("spawn", self.udid, "launchctl", "print", f"pid/{self._pid}")
        if getattr(result, "returncode", 1) != 0:
            detail = _stderr(result) or _stdout(result) or "app exited"
            raise AppCrashedError(detail)

    def _prepare_bundle(self) -> Path:
        configured = self._configured_path()
        if configured is None:
            command = (self.target.build_command or "").strip()
            if not command:
                raise AppMissingError("app path is missing")
            result = self._run(["/bin/sh", "-c", command], timeout=self._command_timeout)
            if getattr(result, "returncode", 1) != 0:
                detail = _stderr(result) or _stdout(result) or "build command failed"
                raise AppMissingError(detail)
            discovered = _discover_app_path(_stdout(result) + "\n" + _stderr(result))
            if not discovered:
                raise AppMissingError(
                    "build command finished but did not report a .app path; set app.path"
                )
            bundle = Path(discovered)
        else:
            bundle = Path(configured)
        if not bundle.exists():
            raise AppMissingError(f"app not found: {bundle}")
        if not bundle.is_dir() or not bundle.name.endswith(".app"):
            raise AppMissingError(f"app bundle is missing or not a .app: {bundle}")
        self._bundle_path = bundle
        info = _read_plist(self._plist_path(bundle))
        bundle_id = _plist_str(info, "CFBundleIdentifier") or (self.target.bundle_id or "").strip()
        if not bundle_id:
            raise AppMissingError("set app.bundle_id or include CFBundleIdentifier in Info.plist")
        self._bundle_id = bundle_id
        return bundle

    def _configured_path(self) -> str | None:
        path = (self.target.path or "").strip()
        return path or None

    def _plist_path(self, bundle: Path | None) -> Path:
        if bundle is None:
            return Path("Info.plist")
        root = bundle / "Info.plist"
        if root.is_file():
            return root
        return bundle / "Contents" / "Info.plist"

    def _claim_simulator(self) -> str:
        pinned = self._udid_override or (os.environ.get(_UDID_ENV) or "").strip()
        if pinned:
            return pinned
        pool = list(dict.fromkeys(item.strip() for item in self.target.simulators if item.strip()))
        if not pool:
            return (self.target.simulator or "").strip() or _DEFAULT_DEVICE
        # Host-wide lease (flock under ~/.aqa/locks), shared with every
        # campaign on this Mac and with IOSSimulatorPool. Locks are keyed by
        # UDID, as the pool keys them, so a name and its UDID cannot be leased
        # twice. It drops when this process exits, so a crashed worker never
        # strands a simulator.
        document: dict | None = None
        for name in pool:
            if _is_udid(name):
                udid = name
            else:
                if document is None:
                    document = self._available_devices()
                udid = select_simulator(document, name)
            lock = try_lock(simulator_lock_name(udid))
            if lock is None:
                continue
            self._lock = lock
            return udid
        raise BackendUnavailable(
            "No free iOS Simulator in app.simulators. "
            "Add one device name or UDID per worker."
        )

    def _release_lock(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def _resolve_udid(self, token: str) -> str:
        if _is_udid(token):
            self.udid = token
            return token
        udid = select_simulator(self._available_devices(), token)
        self.udid = udid
        return udid

    def _available_devices(self) -> dict:
        result = self._simctl("list", "devices", "available", "-j")
        self._check_tool(result, "simctl list")
        try:
            return json.loads(_stdout(result) or "{}")
        except json.JSONDecodeError as exc:
            raise BackendUnavailable("simctl list did not return a device list") from exc

    def _boot(self, udid: str) -> None:
        result = self._simctl("boot", udid)
        if getattr(result, "returncode", 1) == 0:
            self._bootstatus(udid)
            return
        detail = (_stderr(result) + "\n" + _stdout(result)).lower()
        if "booted" in detail:
            self._bootstatus(udid)
            return
        raise BackendUnavailable(
            _stderr(result) or _stdout(result) or f"failed to boot Simulator {udid}"
        )

    def _bootstatus(self, udid: str) -> None:
        result = self._simctl("bootstatus", udid, "-b")
        if getattr(result, "returncode", 1) != 0:
            raise BackendUnavailable(
                _stderr(result) or _stdout(result) or f"Simulator {udid} did not finish booting"
            )

    def _install(self, udid: str, bundle: Path) -> None:
        result = self._simctl("install", udid, str(bundle))
        if getattr(result, "returncode", 1) != 0:
            detail = _stderr(result) or _stdout(result) or f"failed to install {bundle}"
            raise AppMissingError(detail)

    def _start(self, udid: str) -> None:
        args = ["launch", udid, self._bundle_id or ""]
        args.extend(self.target.launch_args)
        env = self._child_env()
        result = self._simctl(*args, env=env)
        if getattr(result, "returncode", 1) != 0:
            self._raise_for_launch(result)
        pid = _pid_from_launch(_stdout(result))
        if pid is None:
            raise AppCrashedError(_stdout(result) or "simctl launch did not report a pid")
        self._pid = pid
        self.udid = udid
        self._assert_running()

    def _child_env(self) -> dict[str, str]:
        merged = {key: str(value) for key, value in os.environ.items()}
        for key, value in self.target.env.items():
            merged[f"SIMCTL_CHILD_{key}"] = str(value)
        return merged

    def _raise_for_launch(self, result: Any) -> None:
        err = _stderr(result) or _stdout(result)
        low = err.lower()
        if any(token in low for token in ("crash", "exited", "sigabrt", "sigkill", "terminated")):
            raise AppCrashedError(err or "app crashed on launch")
        raise AppMissingError(err or f"failed to launch {self._bundle_id}")

    def _terminate_app(self) -> None:
        if self.udid and self._bundle_id and (self.launched or self._pid is not None):
            try:
                self._simctl("terminate", self.udid, self._bundle_id)
            except (AQAError, OSError):
                pass
        self._pid = None

    def _scroll_origin(self, target: ElementQuery | None) -> tuple[float, float]:
        if target is not None:
            element = find_element(self.accessibility_tree(), target)
            if element.frame is not None:
                return _center(element.frame)
        for element in self.accessibility_tree():
            if element.frame is not None:
                return _center(element.frame)
        return (195.0, 422.0)

    def _idb(self, *args: str) -> Any:
        self._require_idb()
        return self._run(["idb", "--udid", self.udid or "", *args], timeout=self._command_timeout)

    def _idb_ok(self, action: str, *args: str) -> None:
        self._require_launched()
        result = self._idb(*args)
        self._check_action(result, action)

    def _simctl(self, *args: str, env: dict[str, str] | None = None) -> Any:
        return self._run(["xcrun", "simctl", *args], env=env, timeout=self._command_timeout)

    def _check_tool(self, result: Any, action: str) -> None:
        if getattr(result, "returncode", 1) != 0:
            detail = _stderr(result) or _stdout(result) or f"{action} failed"
            raise BackendUnavailable(detail)

    def _check_action(self, result: Any, action: str) -> None:
        code = getattr(result, "returncode", 0)
        if code == 0:
            return
        err = _stderr(result) or _stdout(result)
        low = err.lower()
        if any(token in low for token in ("exited", "crash", "not running", "terminated")):
            raise AppCrashedError(err or f"app crashed during {action}")
        raise AQAError(err or f"{action} failed")

    def _run(self, args: list[str], **kwargs: Any) -> Any:
        try:
            return self.runner(list(args), **kwargs)
        except FileNotFoundError as exc:
            missing = str(exc.filename or (args[0] if args else "command"))
            raise BackendUnavailable(
                f"Could not run {missing}. Use the fake driver or a Mac with Xcode."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise UITimeoutError(f"timed out running {args[0] if args else 'command'}") from exc

    def _tool_path(self, name: str) -> str | None:
        try:
            result = self._run(["which", name], timeout=self._command_timeout)
        except (BackendUnavailable, AQAError, OSError):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        found = [line.strip() for line in _stdout(result).splitlines() if line.strip()]
        return found[0] if found else name

    def _media_dir(self) -> Path:
        media = self.work_dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        return media

    def _stop_process(self, proc: Any) -> None:
        # simctl io recordVideo finalizes the movie on SIGINT. SIGKILL drops the file.
        send = getattr(proc, "send_signal", None)
        if send is not None:
            try:
                send(signal.SIGINT)
            except OSError:
                return
        else:
            kill = getattr(proc, "kill", None)
            if kill is not None:
                try:
                    kill()
                except OSError:
                    return
        wait = getattr(proc, "wait", None)
        if wait is not None:
            try:
                wait(timeout=2)
            except Exception:
                return

    def _resolve(self, target: ElementQuery) -> UIElement:
        self._require_launched()
        return find_element(self.accessibility_tree(), target)


def _pid_from_launch(text: str) -> int | None:
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        piece = line.rsplit(":", 1)[-1].strip()
        if piece.isdigit():
            return int(piece)
    return None
