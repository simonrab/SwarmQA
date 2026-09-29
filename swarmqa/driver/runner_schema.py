"""Types and JSON codecs for the swarm runner wire protocol, version 1.

`agents/swarm-runner/PROTOCOL.md` is the prose spec. This module and that
file change together. Encoders return plain dicts ready for `json.dumps`;
decoders accept parsed JSON and raise `RunnerProtocolError` on bad shapes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

from swarmqa.models import ElementQuery, UIElement

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8765
PORT_ENV = "SWARM_RUNNER_PORT"
PROTOCOL_HEADER = "X-Swarm-Protocol"

RunnerPlatform = Literal["ios", "macos"]
AppState = Literal["not_running", "running", "crashed"]
ScreenshotFormat = Literal["png", "jpeg", "none"]
ErrorCode = Literal[
    "bad_request",
    "not_found",
    "not_launched",
    "app_crashed",
    "timeout",
    "protocol_mismatch",
    "internal",
]
ERROR_STATUS: dict[str, int] = {
    "bad_request": 400,
    "not_found": 404,
    "not_launched": 409,
    "app_crashed": 409,
    "timeout": 504,
    "protocol_mismatch": 426,
    "internal": 500,
}
ENDPOINTS: dict[str, str] = {
    "health": "GET /health",
    "launch": "POST /launch",
    "terminate": "POST /terminate",
    "tree": "GET /tree",
    "observe": "POST /observe",
    "tap": "POST /tap",
    "type": "POST /type",
    "swipe": "POST /swipe",
    "key": "POST /key",
    "screenshot": "GET /screenshot",
}


class RunnerProtocolError(ValueError):
    """A message did not match the wire protocol."""


@dataclass(eq=False)
class RunnerError(Exception):
    """An error body returned by the runner."""

    code: ErrorCode
    message: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


@dataclass
class Health:
    protocol_version: int
    runner_version: str
    platform: RunnerPlatform
    app_state: AppState


@dataclass
class LaunchRequest:
    bundle_id: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    terminate_existing: bool = True


@dataclass
class ObserveRequest:
    screenshot: ScreenshotFormat = "png"
    jpeg_quality: float = 0.7


@dataclass
class Screenshot:
    format: Literal["png", "jpeg"]
    data: bytes


@dataclass
class ObserveResponse:
    ts: float
    elements: list[UIElement]
    size: tuple[float, float] | None = None
    scale: float = 1.0
    screenshot: Screenshot | None = None


@dataclass
class TapRequest:
    """Tap a point or an element. Exactly one of `point` and `query` is set."""

    point: tuple[float, float] | None = None
    query: ElementQuery | None = None


@dataclass
class TypeRequest:
    text: str
    query: ElementQuery | None = None


@dataclass
class SwipeRequest:
    start: tuple[float, float]
    end: tuple[float, float]
    duration_s: float = 0.3


@dataclass
class KeyRequest:
    keys: list[str]


# Elements and queries


def encode_element(element: UIElement) -> dict[str, Any]:
    return {
        "role": element.role,
        "label": element.label or None,
        "identifier": element.identifier,
        "value": element.value,
        "enabled": element.enabled,
        "frame": list(element.frame) if element.frame is not None else None,
        "children": [encode_element(child) for child in element.children],
    }


def decode_element(data: Any) -> UIElement:
    obj = _object(data, "element")
    role = obj.get("role")
    if not isinstance(role, str) or not role:
        raise RunnerProtocolError("element.role must be a non-empty string")
    frame = obj.get("frame")
    return UIElement(
        role=role,
        label=_optional_str(obj, "label") or "",
        identifier=_optional_str(obj, "identifier"),
        value=_optional_str(obj, "value"),
        enabled=bool(obj.get("enabled", True)),
        frame=_frame(frame) if frame is not None else None,
        children=[decode_element(child) for child in _list(obj.get("children", []), "children")],
    )


def encode_query(query: ElementQuery) -> dict[str, Any]:
    encoded = {
        "role": query.role,
        "label": query.label,
        "identifier": query.identifier,
        "value": query.value,
    }
    if not any(encoded.values()):
        raise RunnerProtocolError("query needs at least one field")
    return encoded


def decode_query(data: Any) -> ElementQuery:
    obj = _object(data, "query")
    query = ElementQuery(
        role=_optional_str(obj, "role"),
        label=_optional_str(obj, "label"),
        identifier=_optional_str(obj, "identifier"),
        value=_optional_str(obj, "value"),
    )
    if not any((query.role, query.label, query.identifier, query.value)):
        raise RunnerProtocolError("query needs at least one field")
    return query


# Requests


def encode_launch(request: LaunchRequest) -> dict[str, Any]:
    return {
        "bundle_id": request.bundle_id,
        "args": list(request.args),
        "env": dict(request.env),
        "terminate_existing": request.terminate_existing,
    }


def encode_observe(request: ObserveRequest) -> dict[str, Any]:
    return {"screenshot": request.screenshot, "jpeg_quality": request.jpeg_quality}


def encode_tap(request: TapRequest) -> dict[str, Any]:
    if (request.point is None) == (request.query is None):
        raise RunnerProtocolError("tap needs exactly one of point and query")
    if request.point is not None:
        return {"x": float(request.point[0]), "y": float(request.point[1])}
    return {"query": encode_query(request.query)}  # type: ignore[arg-type]


def encode_type(request: TypeRequest) -> dict[str, Any]:
    return {
        "text": request.text,
        "query": encode_query(request.query) if request.query is not None else None,
    }


def encode_swipe(request: SwipeRequest) -> dict[str, Any]:
    return {
        "from": [float(request.start[0]), float(request.start[1])],
        "to": [float(request.end[0]), float(request.end[1])],
        "duration_s": request.duration_s,
    }


def encode_key(request: KeyRequest) -> dict[str, Any]:
    if not request.keys:
        raise RunnerProtocolError("key needs at least one key")
    return {"keys": list(request.keys)}


# Responses


def decode_health(data: Any) -> Health:
    obj = _object(data, "health")
    version = obj.get("protocol_version")
    if not isinstance(version, int):
        raise RunnerProtocolError("health.protocol_version must be an integer")
    platform = obj.get("platform")
    if platform not in ("ios", "macos"):
        raise RunnerProtocolError("health.platform must be ios or macos")
    state = obj.get("app_state", "not_running")
    if state not in ("not_running", "running", "crashed"):
        raise RunnerProtocolError("health.app_state is not a known state")
    return Health(
        protocol_version=version,
        runner_version=str(obj.get("runner_version", "")),
        platform=platform,
        app_state=state,
    )


def decode_tree(data: Any) -> tuple[float, list[UIElement]]:
    obj = _object(data, "tree")
    return _ts(obj), [decode_element(item) for item in _list(obj.get("elements"), "elements")]


def encode_observe_response(response: ObserveResponse) -> dict[str, Any]:
    """Runner side of /observe. Used by fakes and tests."""
    shot = None
    if response.screenshot is not None:
        shot = {
            "format": response.screenshot.format,
            "data": base64.b64encode(response.screenshot.data).decode("ascii"),
        }
    return {
        "ts": response.ts,
        "elements": [encode_element(element) for element in response.elements],
        "size": list(response.size) if response.size is not None else None,
        "scale": response.scale,
        "screenshot": shot,
    }


def decode_observe_response(data: Any) -> ObserveResponse:
    obj = _object(data, "observe")
    ts, elements = decode_tree(obj)
    size = obj.get("size")
    shot = obj.get("screenshot")
    screenshot = None
    if shot is not None:
        shot_obj = _object(shot, "screenshot")
        fmt = shot_obj.get("format")
        if fmt not in ("png", "jpeg"):
            raise RunnerProtocolError("screenshot.format must be png or jpeg")
        try:
            raw = base64.b64decode(str(shot_obj.get("data", "")), validate=True)
        except ValueError as exc:
            raise RunnerProtocolError("screenshot.data is not base64") from exc
        screenshot = Screenshot(format=fmt, data=raw)
    scale = obj.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise RunnerProtocolError("observe.scale must be a number")
    return ObserveResponse(
        ts=ts,
        elements=elements,
        size=_pair(size, "size") if size is not None else None,
        scale=float(scale),
        screenshot=screenshot,
    )


def encode_error(error: RunnerError) -> dict[str, Any]:
    return {"error": {"code": error.code, "message": error.message}}


def decode_error(data: Any) -> RunnerError:
    """Decode an error body. Unknown shapes become `internal`."""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        body = data["error"]
        code = body.get("code")
        if code in ERROR_STATUS:
            return RunnerError(code=code, message=str(body.get("message", "")))
        return RunnerError(code="internal", message=str(body.get("message", code or "")))
    return RunnerError(code="internal", message=str(data)[:200])


# Helpers


def _object(data: Any, name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RunnerProtocolError(f"{name} must be an object")
    return data


def _list(data: Any, name: str) -> list[Any]:
    if not isinstance(data, list):
        raise RunnerProtocolError(f"{name} must be a list")
    return data


def _optional_str(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunnerProtocolError(f"{key} must be a string or null")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerProtocolError(f"{name} must be a number")
    return float(value)


def _frame(value: Any) -> tuple[float, float, float, float]:
    items = _list(value, "frame")
    if len(items) != 4:
        raise RunnerProtocolError("frame must have 4 numbers")
    x, y, w, h = (_number(item, "frame") for item in items)
    return (x, y, w, h)


def _pair(value: Any, name: str) -> tuple[float, float]:
    items = _list(value, name)
    if len(items) != 2:
        raise RunnerProtocolError(f"{name} must have 2 numbers")
    return (_number(items[0], name), _number(items[1], name))


def _ts(obj: dict[str, Any]) -> float:
    return _number(obj.get("ts"), "ts")
