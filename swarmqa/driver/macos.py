"""macOS Accessibility driver.

One instance drives one app session for one worker. Subprocess calls go through
``self.runner`` so Linux tests can supply System Events output without a display.
On a Mac the default runner is ``subprocess``. Importing this module does not
require macOS, a display, or Accessibility permission.

Grant Accessibility permission to the process that launches ``osascript``
(Terminal, Cursor, or the Python host) before a real session. See
``docs/driver.md``.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

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
    "MacOSDriver cannot launch apps on this host. Use the fake driver or a Mac host."
)

_ROLE_MAP = {
    "window": "window",
    "axwindow": "window",
    "button": "button",
    "axbutton": "button",
    "textfield": "textfield",
    "axtextfield": "textfield",
    "textarea": "textarea",
    "axtextarea": "textarea",
    "menu": "menu",
    "axmenu": "menu",
    "menuitem": "menuitem",
    "axmenuitem": "menuitem",
    "menubar": "menubar",
    "axmenubar": "menubar",
    "menubaritem": "menuitem",
    "axmenubaritem": "menuitem",
    "checkbox": "checkbox",
    "axcheckbox": "checkbox",
    "statictext": "text",
    "axstatictext": "text",
    "group": "group",
    "axgroup": "group",
    "scrollarea": "scrollarea",
    "axscrollarea": "scrollarea",
    "popupbutton": "popupbutton",
    "axpopupbutton": "popupbutton",
    "radiobutton": "radiobutton",
    "axradiobutton": "radiobutton",
}

_AX_ROLES = {
    "window": ["AXWindow"],
    "button": ["AXButton"],
    "textfield": ["AXTextField", "AXTextArea"],
    "textarea": ["AXTextArea", "AXTextField"],
    "menu": ["AXMenu"],
    "menuitem": ["AXMenuItem", "AXMenuBarItem"],
    "menubar": ["AXMenuBar"],
    "checkbox": ["AXCheckBox"],
    "text": ["AXStaticText"],
    "group": ["AXGroup"],
    "scrollarea": ["AXScrollArea"],
    "popupbutton": ["AXPopUpButton"],
    "radiobutton": ["AXRadioButton"],
}

_KEY_CODES = {
    "return": 36,
    "enter": 76,
    "esc": 53,
    "escape": 53,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
}

_MODIFIERS = {
    "cmd": "command down",
    "command": "command down",
    "shift": "shift down",
    "option": "option down",
    "alt": "option down",
    "opt": "option down",
    "ctrl": "control down",
    "control": "control down",
}

_APP_PATH = re.compile(r"((?:~|/|\.{1,2}/)[^\s\"']+?\.app)\b")


def _default_runner(
    args: list[str],
    *,
    background: bool = False,
    input: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Any:
    """Run a command, or spawn it when ``background`` is set.

    A background handle exposes ``pid``, ``poll``, ``wait``, and ``kill``.
    Foreground calls return ``subprocess.CompletedProcess`` with text output.
    """
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


def _as_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return f'"{escaped}"'


def _canonical_role(role: str) -> str:
    text = (role or "").strip()
    key = text.lower().replace(" ", "")
    mapped = _ROLE_MAP.get(key)
    if mapped:
        return mapped
    if key.startswith("ax") and len(key) > 2:
        return key[2:]
    return text or "unknown"


def _ax_role_names(role: str) -> list[str]:
    canonical = _canonical_role(role)
    if canonical in _AX_ROLES:
        return list(_AX_ROLES[canonical])
    if role.startswith("AX"):
        return [role]
    return [f"AX{canonical[:1].upper()}{canonical[1:]}"]


def parse_accessibility_dump(text: str) -> list[UIElement]:
    """Parse the tab-separated tree emitted by the System Events script.

    Columns are depth, role, label, identifier, value, enabled, x, y, width,
    height. Depth nests each node under the nearest shallower ancestor.
    """
    roots: list[UIElement] = []
    stack: list[tuple[int, UIElement]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("--"):
            continue
        parts = raw.split("\t")
        depth_text = parts[0].strip()
        if not re.fullmatch(r"-?\d+", depth_text):
            continue
        depth = int(depth_text)
        role = _canonical_role(parts[1] if len(parts) > 1 else "")
        label = parts[2] if len(parts) > 2 else ""
        identifier = parts[3] if len(parts) > 3 and parts[3] else None
        value = parts[4] if len(parts) > 4 else None
        enabled = True
        if len(parts) > 5 and parts[5].strip().lower() in {"0", "false"}:
            enabled = False
        frame = None
        if len(parts) >= 10:
            try:
                frame = tuple(float(parts[index]) for index in range(6, 10))
            except ValueError:
                frame = None
        node = UIElement(
            role=role,
            label=label,
            identifier=identifier,
            value=value,
            enabled=enabled,
            frame=frame,
        )
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((depth, node))
    return roots


def _discover_app_path(text: str) -> str | None:
    match = _APP_PATH.search(text)
    if match:
        return match.group(1)
    for token in text.split():
        if token.endswith(".app"):
            return token
    return None


def _as_bundle(path: Path) -> Path:
    if path.name.endswith(".app"):
        return path
    for parent in path.parents:
        if parent.name.endswith(".app"):
            return parent
    return path


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


def _expand_keys(keys: list[str]) -> list[str]:
    parts: list[str] = []
    for key in keys:
        parts.extend(piece.strip() for piece in key.split("+") if piece.strip())
    return parts


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")
    return (cleaned or "screenshot")[:80]


def _menu_expression(path: list[str]) -> str:
    if len(path) == 1:
        return f"menu bar item {_as_string(path[0])} of menu bar 1"
    expr = f"menu item {_as_string(path[-1])}"
    for name in reversed(path[1:-1]):
        expr = f"{expr} of menu 1 of menu item {_as_string(name)}"
    return f"{expr} of menu 1 of menu bar item {_as_string(path[0])} of menu bar 1"


_SANITIZE_HANDLER = """on sanitize(rawValue)
  set t to ""
  try
    set t to rawValue as text
  on error
    return ""
  end try
  set saved to AppleScript's text item delimiters
  set AppleScript's text item delimiters to {tab, linefeed, return}
  set chunks to text items of t
  set AppleScript's text item delimiters to " "
  set t to chunks as text
  set AppleScript's text item delimiters to saved
  return t
end sanitize
"""


def _append_element(var: str, depth: int) -> str:
    """Inline a tree row. UI elements stay in the caller's tell block.

    System Events rejects UI element references passed into handlers, so the
    property reads happen on ``var`` directly. ``sanitize`` only receives text.
    """
    return f"""set r to ""
try
  set r to role of {var} as text
end try
set n to ""
try
  set n to my sanitize(name of {var})
end try
set rowId to ""
try
  set rowId to my sanitize(value of attribute "AXIdentifier" of {var})
end try
set v to ""
try
  set v to my sanitize(value of {var})
end try
set en to "1"
try
  if enabled of {var} is false then set en to "0"
end try
set xs to "0"
set ys to "0"
set ws to "0"
set hs to "0"
try
  set pos to position of {var}
  set xs to (item 1 of pos) as text
  set ys to (item 2 of pos) as text
  set sz to size of {var}
  set ws to (item 1 of sz) as text
  set hs to (item 2 of sz) as text
end try
set output to output & ("{depth}" & tab & r & tab & n & tab & rowId & tab & v & tab & en & tab & xs & tab & ys & tab & ws & tab & hs & linefeed)
"""


def _tree_script(process: str) -> str:
    proc = _as_string(process)
    window = _append_element("w", 0)
    child = _append_element("el", 1)
    menu = _append_element("mb", 0)
    return (
        "-- SWARMQA_TREE\n"
        + _SANITIZE_HANDLER
        + 'set output to ""\n'
        + 'tell application "System Events"\n'
        + f"  tell process {proc}\n"
        + "    set frontmost to true\n"
        + "    repeat with w in windows\n"
        + window
        + "      try\n"
        + "        repeat with el in entire contents of w\n"
        + child
        + "        end repeat\n"
        + "      end try\n"
        + "    end repeat\n"
        + "    try\n"
        + "      repeat with mb in menu bars\n"
        + menu
        + "        try\n"
        + "          repeat with el in entire contents of mb\n"
        + child
        + "          end repeat\n"
        + "        end try\n"
        + "      end repeat\n"
        + "    end try\n"
        + "  end tell\n"
        + "end tell\n"
        + "return output\n"
    )


def _alive_script(process: str) -> str:
    proc = _as_string(process)
    return f"""-- SWARMQA_ALIVE
tell application "System Events"
  if not (exists process {proc}) then error "process exited"
  return unix id of process {proc}
end tell
"""


def _quit_script(process: str, bundle_id: str | None) -> str:
    proc = _as_string(process)
    if bundle_id:
        quit_line = f"tell application id {_as_string(bundle_id)} to quit"
    else:
        quit_line = f"tell application {proc} to quit"
    return f"""-- SWARMQA_QUIT
tell application "System Events"
  if exists process {proc} then
    set frontmost of process {proc} to true
  end if
end tell
try
  {quit_line}
end try
"""


def _match_helpers(element: UIElement) -> str:
    roles = ", ".join(_as_string(role) for role in _ax_role_names(element.role))
    identifier = element.identifier or ""
    label = element.label or ""
    use_value = element.value is not None and not identifier and not label
    value_text = element.value if use_value and element.value is not None else ""
    use_flag = "true" if use_value else "false"
    return f"""set ident to {_as_string(identifier)}
set labelText to {_as_string(label)}
set valueText to {_as_string(value_text)}
set useValue to {use_flag}
set roleNames to {{{roles}}}
"""


def _match_lines(var: str) -> str:
    """Set ``matched`` by reading ``var`` in the current System Events tell."""
    return f"""set matched to false
set r to ""
try
  set r to role of {var} as text
end try
set roleOk to false
repeat with candidate in roleNames
  if r is (candidate as text) then set roleOk to true
end repeat
if roleOk then
  if ident is not "" then
    set actualId to ""
    try
      set actualId to value of attribute "AXIdentifier" of {var} as text
    end try
    if actualId is ident then set matched to true
  else if labelText is not "" then
    set actualName to ""
    try
      set actualName to name of {var} as text
    end try
    if actualName is labelText then
      set matched to true
    else
      try
        if actualName contains labelText then set matched to true
      end try
    end if
  else if useValue then
    set actualValue to ""
    try
      set actualValue to value of {var} as text
    end try
    if actualValue is valueText then set matched to true
  else
    set matched to true
  end if
end if
"""


def _search_act(process: str, action_for: Callable[[str], str]) -> str:
    """Find the element and run ``action_for(var)`` on that loop variable.

    The action runs inside the repeat so System Events still has the reference.
    """
    proc = _as_string(process)

    def once(var: str) -> str:
        return _match_lines(var) + (
            "if matched and didAct is false then\n"
            + action_for(var)
            + "\nset didAct to true\n"
            "end if\n"
        )

    return (
        "set didAct to false\n"
        'tell application "System Events"\n'
        f"  tell process {proc}\n"
        "    set frontmost to true\n"
        "    repeat with w in windows\n"
        "      if didAct is false then\n"
        + once("w")
        + "      end if\n"
        "      if didAct is false then\n"
        "        try\n"
        "          repeat with el in entire contents of w\n"
        "            if didAct is false then\n"
        + once("el")
        + "            end if\n"
        "          end repeat\n"
        "        end try\n"
        "      end if\n"
        "    end repeat\n"
        "    if didAct is false then\n"
        "      try\n"
        "        repeat with mb in menu bars\n"
        "          if didAct is false then\n"
        "            try\n"
        "              repeat with el in entire contents of mb\n"
        "                if didAct is false then\n"
        + once("el")
        + "                end if\n"
        "              end repeat\n"
        "            end try\n"
        "          end if\n"
        "        end repeat\n"
        "      end try\n"
        "    end if\n"
        "  end tell\n"
        "end tell\n"
        'if didAct is false then error "element not found"\n'
    )


def _click_script(process: str, element: UIElement) -> str:
    ident = element.identifier or ""
    return (
        "-- SWARMQA_CLICK\n"
        f"-- role={element.role} label={element.label} identifier={ident}\n"
        + _match_helpers(element)
        + _search_act(process, lambda var: f'perform action "AXPress" of {var}')
    )


def _type_script(process: str, element: UIElement, text: str) -> str:
    ident = element.identifier or ""
    quoted = _as_string(text)

    def action(var: str) -> str:
        return "set focused of " + var + " to true\nset value of " + var + " to " + quoted

    return (
        "-- SWARMQA_TYPE\n"
        f"-- role={element.role} label={element.label} identifier={ident}\n"
        + _match_helpers(element)
        + _search_act(process, action)
    )


def _key_script(process: str, keys: list[str]) -> str:
    parts = _expand_keys(keys)
    modifiers: list[str] = []
    key: str | None = None
    for part in parts:
        modifier = _MODIFIERS.get(part.lower())
        if modifier:
            if modifier not in modifiers:
                modifiers.append(modifier)
        else:
            key = part
    if key is None:
        raise AQAError("keychord is missing a key")
    using = ""
    if modifiers:
        using = " using {" + ", ".join(modifiers) + "}"
    named = _KEY_CODES.get(key.lower())
    if named is not None:
        command = f"key code {named}{using}"
    else:
        command = f"keystroke {_as_string(key)}{using}"
    rendered = "+".join(parts)
    return f"""-- SWARMQA_KEY
-- keys={rendered}
tell application "System Events"
  tell process {_as_string(process)} to set frontmost to true
  {command}
end tell
"""


def _scroll_script(process: str, delta: int) -> str:
    steps = abs(int(delta))
    code = 125 if delta > 0 else 126
    repeat = ""
    if steps:
        repeat = f"""repeat {steps} times
    key code {code}
  end repeat
"""
    return f"""-- SWARMQA_SCROLL
-- delta={int(delta)}
tell application "System Events"
  tell process {_as_string(process)} to set frontmost to true
  {repeat}end tell
"""


def _menu_script(process: str, path: list[str]) -> str:
    expr = _menu_expression(path)
    rendered = " > ".join(path)
    return f"""-- SWARMQA_MENU
-- path={rendered}
tell application "System Events"
  tell process {_as_string(process)}
    set frontmost to true
    click {expr}
  end tell
end tell
"""


class MacOSDriver:
    """Drive a macOS ``.app`` through System Events and ``osascript``.

    ``runner`` replaces every subprocess call. Tests pass a callable with the
    same shape as :func:`_default_runner`. ``platform`` defaults to
    ``sys.platform``; Linux tests that exercise launch pass ``platform="darwin"``.
    """

    def __init__(
        self,
        target: AppTarget,
        work_dir: Path,
        video_mode: str = "always",
        *,
        runner: Runner | None = None,
        platform: str | None = None,
    ):
        self.target = target
        self.work_dir = Path(work_dir)
        self.video_mode = video_mode
        self.runner: Runner = runner if runner is not None else _default_runner
        self.platform = sys.platform if platform is None else platform
        self.launched = False
        self._bundle_path: Path | None = None
        self._process_name: str | None = None
        self._proc: Any = None
        self._opened = False
        self._pid: int | None = None
        self._video_on = False
        self._video_proc: Any = None
        self._video_path: Path | None = None
        self._command_timeout = 30.0
        self._poll_s = 0.05
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def launch(self) -> None:
        if self.platform != "darwin":
            raise BackendUnavailable(_NON_DARWIN)
        if self.launched:
            return
        bundle = self._prepare_bundle()
        env = self._child_env()
        binary = self._executable(bundle)
        if binary is not None:
            proc = self._run(
                [str(binary), *self.target.launch_args],
                background=True,
                env=env,
            )
            self._proc = proc
            code = proc.poll() if hasattr(proc, "poll") else getattr(proc, "returncode", None)
            if code is not None:
                raise AppCrashedError(
                    f"{self._process_name} exited during launch with status {code}"
                )
        else:
            args = ["open", "-n", str(bundle)]
            if self.target.launch_args:
                args.extend(["--args", *self.target.launch_args])
            result = self._run(args, env=env)
            if getattr(result, "returncode", 1) != 0:
                self._raise_for_launch(result)
            self._opened = True
            self._assert_running()
        self.launched = True

    def relaunch(self) -> None:
        self._quit_app()
        self.launch()

    def close(self) -> None:
        if self._video_on:
            self.stop_video()
        self._quit_app()
        self.launched = False

    def accessibility_tree(self) -> list[UIElement]:
        self._require_launched()
        result = self._osascript(_tree_script(self._process_name or ""))
        self._check_action(result, "accessibility tree")
        return parse_accessibility_dump(_stdout(result))

    def click(self, target: ElementQuery) -> None:
        element = self._resolve(target)
        self._perform(_click_script(self._process_name or "", element), "click")

    def type_text(self, target: ElementQuery, text: str) -> None:
        element = self._resolve(target)
        self._perform(
            _type_script(self._process_name or "", element, text),
            "type",
        )

    def keychord(self, keys: list[str]) -> None:
        self._require_launched()
        self._perform(_key_script(self._process_name or "", keys), "keychord")

    def scroll(self, delta: int, target: ElementQuery | None = None) -> None:
        self._require_launched()
        if target is not None:
            element = find_element(self.accessibility_tree(), target)
            self._perform(_click_script(self._process_name or "", element), "click")
        self._perform(_scroll_script(self._process_name or "", delta), "scroll")

    def select_menu(self, path: list[str]) -> None:
        self._require_launched()
        if not path:
            raise ElementNotFoundError("menu path is empty")
        self._perform(_menu_script(self._process_name or "", path), "menu")

    def wait_for(self, target: ElementQuery, timeout_s: float) -> UIElement:
        self._require_launched()
        deadline = time.monotonic() + max(timeout_s, 0)
        last_error: ElementNotFoundError | None = None
        while True:
            try:
                return find_element(self.accessibility_tree(), target)
            except ElementNotFoundError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise UITimeoutError(
                        f"timed out after {timeout_s}s waiting for {exc}"
                    ) from exc
                time.sleep(self._poll_s)

    def screenshot(self, name: str) -> Path:
        self._require_launched()
        path = self._media_dir() / f"{_safe_name(name)}.png"
        command = self._screenshot_command(path)
        result = self._run(command, timeout=self._command_timeout)
        self._check_action(result, "screenshot")
        payload = getattr(result, "stdout", b"")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not path.is_file() and isinstance(payload, bytes) and payload.startswith(b"\x89PNG"):
            path.write_bytes(payload)
        if not path.is_file():
            raise AQAError(f"screenshot was not written: {path}")
        if not path.read_bytes().startswith(b"\x89PNG"):
            raise AQAError(f"screenshot is not a PNG: {path}")
        return path

    def start_video(self) -> None:
        self._require_launched()
        if self._video_on:
            return
        media = self._media_dir()
        ffmpeg = self._tool_path("ffmpeg")
        if ffmpeg:
            path = media / "session.mp4"
            self._video_proc = self._run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "avfoundation",
                    "-framerate",
                    "30",
                    "-i",
                    "1:none",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                background=True,
            )
            self._video_path = path
            self._video_on = True
            return
        capture = self._tool_path("screencapture")
        if capture:
            path = media / "session.mov"
            self._video_proc = self._run([capture, "-v", str(path)], background=True)
            self._video_path = path
            self._video_on = True
            return
        note = media / "session.txt"
        note.write_text(
            "video unavailable: neither ffmpeg nor screencapture is installed\n",
            encoding="utf-8",
        )
        self._video_path = note
        self._video_proc = None
        self._video_on = True

    def stop_video(self) -> Path | None:
        proc = self._video_proc
        path = self._video_path
        self._video_on = False
        self._video_proc = None
        if proc is not None:
            self._stop_process(proc, signal="INT")
        return path

    def metadata(self) -> BuildMetadata:
        bundle = self._bundle_path
        if bundle is None:
            configured = self._configured_path()
            bundle = _as_bundle(Path(configured)) if configured else None
        info = _read_plist(bundle / "Contents" / "Info.plist") if bundle else {}
        bundle_id = _plist_str(info, "CFBundleIdentifier") or self.target.bundle_id
        version = _plist_str(info, "CFBundleShortVersionString")
        return BuildMetadata(
            path=str(bundle) if bundle is not None else self.target.path,
            bundle_id=bundle_id,
            version=version,
            backend="macos",
        )

    def _resolve(self, target: ElementQuery) -> UIElement:
        self._require_launched()
        return find_element(self.accessibility_tree(), target)

    def _require_launched(self) -> None:
        if not self.launched:
            raise AppMissingError("app is not launched")
        self._assert_running()

    def _assert_running(self) -> None:
        if self._proc is not None:
            code = self._proc.poll() if hasattr(self._proc, "poll") else None
            if code is not None:
                raise AppCrashedError(
                    f"{self._process_name or 'app'} exited with status {code}"
                )
            return
        if not self._opened:
            return
        result = self._osascript(_alive_script(self._process_name or ""))
        if getattr(result, "returncode", 1) != 0:
            detail = _stderr(result) or _stdout(result) or f"{self._process_name or 'app'} exited"
            raise AppCrashedError(detail)
        text = _stdout(result).strip().splitlines()
        if text and text[0].strip().isdigit():
            self._pid = int(text[0].strip())

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
            bundle = _as_bundle(Path(configured))
        if not bundle.exists():
            raise AppMissingError(f"app not found: {bundle}")
        if not bundle.is_dir() or not bundle.name.endswith(".app"):
            raise AppMissingError(f"app bundle is missing or not a .app: {bundle}")
        self._bundle_path = bundle
        self._process_name = self._read_process_name(bundle)
        return bundle

    def _configured_path(self) -> str | None:
        path = (self.target.path or "").strip()
        return path or None

    def _read_process_name(self, bundle: Path) -> str:
        info = _read_plist(bundle / "Contents" / "Info.plist")
        for key in ("CFBundleExecutable", "CFBundleName"):
            value = _plist_str(info, key)
            if value:
                return value
        return bundle.stem

    def _executable(self, bundle: Path) -> Path | None:
        info = _read_plist(bundle / "Contents" / "Info.plist")
        name = _plist_str(info, "CFBundleExecutable") or bundle.stem
        binary = bundle / "Contents" / "MacOS" / name
        if binary.is_file():
            return binary
        return None

    def _bundle_id(self) -> str | None:
        bundle = self._bundle_path
        if bundle is None:
            return self.target.bundle_id
        info = _read_plist(bundle / "Contents" / "Info.plist")
        return _plist_str(info, "CFBundleIdentifier") or self.target.bundle_id

    def _child_env(self) -> dict[str, str]:
        merged = {key: str(value) for key, value in os.environ.items()}
        merged.update({key: str(value) for key, value in self.target.env.items()})
        return merged

    def _raise_for_launch(self, result: Any) -> None:
        err = _stderr(result) or _stdout(result)
        low = err.lower()
        if any(token in low for token in ("crash", "exited", "segmentation", "sigabrt", "sigkill")):
            raise AppCrashedError(err or "app crashed on launch")
        if any(token in low for token in ("code signature", "unsigned", "not signed")):
            raise AppMissingError(err or f"app is unsigned: {self._bundle_path}")
        raise AppMissingError(err or f"failed to launch {self._bundle_path}")

    def _quit_app(self) -> None:
        if self._process_name and (self.launched or self._opened):
            try:
                self._osascript(_quit_script(self._process_name, self._bundle_id()))
            except (AQAError, OSError):
                pass
        if self._proc is not None:
            self._stop_process(self._proc, signal="TERM")
        self._proc = None
        self._opened = False
        self.launched = False

    def _perform(self, script: str, action: str) -> None:
        result = self._osascript(script)
        self._check_action(result, action)

    def _check_action(self, result: Any, action: str) -> None:
        code = getattr(result, "returncode", 0)
        if code == 0:
            self._assert_running()
            return
        err = _stderr(result) or _stdout(result)
        low = err.lower()
        if "element not found" in low:
            raise ElementNotFoundError(err or f"element not found during {action}")
        if any(
            token in low
            for token in (
                "assistive",
                "not authorized",
                "not authorised",
                "accessibility",
            )
        ) or "1002" in err:
            raise BackendUnavailable(
                "Accessibility permission is required for System Events. "
                "Grant access in System Settings → Privacy & Security → Accessibility "
                "for the terminal or agent that runs osascript, or use the fake driver. "
                f"Original error: {err}"
            )
        if any(
            token in low
            for token in (
                "exited",
                "crash",
                "not running",
                "isn't running",
                "is not running",
                "-600",
                "connection invalid",
                "invalid connection",
            )
        ):
            raise AppCrashedError(err or f"{self._process_name or 'app'} crashed during {action}")
        raise AQAError(err or f"{action} failed")

    def _osascript(self, script: str) -> Any:
        return self._run(["osascript", "-"], input=script, timeout=self._command_timeout)

    def _run(self, args: list[str], **kwargs: Any) -> Any:
        try:
            return self.runner(list(args), **kwargs)
        except FileNotFoundError as exc:
            missing = str(exc.filename or (args[0] if args else "command"))
            if "Contents/MacOS" in missing:
                raise AppMissingError(f"app executable not found: {missing}") from exc
            raise BackendUnavailable(
                f"Could not run {args[0] if args else 'command'}. "
                "Use the fake driver or a Mac host."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise UITimeoutError(f"timed out running {args[0] if args else 'command'}") from exc

    def _tool_path(self, name: str) -> str | None:
        try:
            result = self._run(["which", name], timeout=self._command_timeout)
        except (BackendUnavailable, AQAError, OSError):
            result = None
        if result is not None and getattr(result, "returncode", 1) == 0:
            found = [line.strip() for line in _stdout(result).splitlines() if line.strip()]
            if found:
                return found[0]
        if (
            name == "screencapture"
            and self.runner is _default_runner
            and Path("/usr/sbin/screencapture").is_file()
        ):
            return "/usr/sbin/screencapture"
        return None

    def _media_dir(self) -> Path:
        media = self.work_dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        return media

    def _screenshot_command(self, path: Path) -> list[str]:
        binary = self._tool_path("screencapture") or "/usr/sbin/screencapture"
        frame = self._front_window_frame()
        if frame is None:
            return [binary, "-x", str(path)]
        x, y, width, height = (int(value) for value in frame)
        if width <= 0 or height <= 0:
            return [binary, "-x", str(path)]
        return [binary, "-x", "-R", f"{x},{y},{width},{height}", str(path)]

    def _front_window_frame(self) -> tuple[float, float, float, float] | None:
        try:
            tree = self.accessibility_tree()
        except (AQAError, OSError):
            return None
        for element in tree:
            if element.role == "window" and element.frame and len(element.frame) == 4:
                return element.frame
        return None

    def _stop_process(self, proc: Any, *, signal: str) -> None:
        pid = getattr(proc, "pid", None)
        if pid:
            flag = "-INT" if signal == "INT" else "-TERM"
            try:
                self._run(["kill", flag, str(pid)], timeout=self._command_timeout)
            except (AQAError, OSError):
                pass
        if hasattr(proc, "wait"):
            try:
                proc.wait(timeout=3)
            except Exception:
                if hasattr(proc, "kill"):
                    proc.kill()
