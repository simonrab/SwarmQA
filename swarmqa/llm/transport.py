"""The seam between a provider and the network.

A `Transport` sends one request dict (the provider's wire format) and
returns the response as a plain dict. SDK transports live next to their
adapters and import the SDK lazily. `ReplayTransport` serves recorded
responses so tests and dry runs never touch the network;
`RecordingTransport` wraps a live transport and saves what it sees.

Transports raise `TransportTimeout` for timeouts and `TransientError` for
retryable failures (connection errors, 408, 409, 429, 5xx). Anything else
is a `ModelError`. Retries happen in the provider, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from swarmqa.llm.protocol import ModelError


class TransportTimeout(Exception):
    """One attempt ran past its timeout."""


class TransientError(Exception):
    """A retryable failure. `retry_after` is seconds from the server, when given."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Transport(Protocol):
    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]: ...


_RETRYABLE_STATUS = {408, 409, 429}


def classify_sdk_error(exc: Exception, sdk: Any) -> Exception:
    """Map an Anthropic or OpenAI SDK exception to a transport error.

    Both SDKs expose `APITimeoutError`, `APIConnectionError` and
    `APIStatusError` (with `status_code` and `response`).
    """
    timeout_cls = getattr(sdk, "APITimeoutError", None)
    connection_cls = getattr(sdk, "APIConnectionError", None)
    status_cls = getattr(sdk, "APIStatusError", None)
    if timeout_cls is not None and isinstance(exc, timeout_cls):
        return TransportTimeout(str(exc) or "timed out")
    if connection_cls is not None and isinstance(exc, connection_cls):
        return TransientError(str(exc) or "connection error")
    if status_cls is not None and isinstance(exc, status_cls):
        status = int(getattr(exc, "status_code", 0) or 0)
        message = f"HTTP {status}: {getattr(exc, 'message', '') or exc}"
        if status in _RETRYABLE_STATUS or status >= 500:
            return TransientError(message, status=status, retry_after=_retry_after(exc))
        return ModelError(message)
    return ModelError(f"{exc.__class__.__name__}: {exc}")


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class ReplayTransport:
    """Serve queued responses in order. An `Exception` item is raised instead.

    Every request is kept in `requests` (with its timeout) for assertions.
    Running out of responses raises `ModelError`.
    """

    responses: list[dict[str, Any] | Exception] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    @classmethod
    def from_files(cls, *paths: Path | str) -> "ReplayTransport":
        return cls([json.loads(Path(path).read_text(encoding="utf-8")) for path in paths])

    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self.requests.append(request)
        self.timeouts.append(timeout_s)
        if not self.responses:
            raise ModelError("replay transport has no more responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class RecordingTransport:
    """Forward to `inner` and save each response as `<directory>/<n>.json`.

    Requests are saved beside them as `<n>.request.json` with image data
    elided, so fixtures stay small and hold no screenshots.
    """

    inner: Transport
    directory: Path
    count: int = 0

    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        response = self.inner.send(request, timeout_s=timeout_s)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.count += 1
        stem = self.directory / f"{self.count:03d}"
        stem.with_suffix(".json").write_text(json.dumps(response, indent=2, sort_keys=True), encoding="utf-8")
        stem.with_suffix(".request.json").write_text(
            json.dumps(_elide_images(request), indent=2, sort_keys=True), encoding="utf-8"
        )
        return response


def _elide_images(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("data", "image_url") and isinstance(item, str) and len(item) > 256:
                out[key] = f"<{len(item)} chars elided>"
            else:
                out[key] = _elide_images(item)
        return out
    if isinstance(value, list):
        return [_elide_images(item) for item in value]
    return value
