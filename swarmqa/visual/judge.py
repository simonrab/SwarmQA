"""Appearance judgment for one saved screenshot.

Pixel comparison stays in ``diff.py``. This module looks at a PNG and
returns ``fine`` or a short written judgment. A bad screen becomes a
``visual_judgment`` finding. Command failures fail open and do not retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

from swarmqa.models import CampaignConfig, Finding, VisualJudgmentConfig
from swarmqa.reporter.findings import fingerprint_for

_TITLE_LIMIT = 120
_FINE = "fine"


class JudgeError(Exception):
    """The command could not produce a usable judgment."""


def judge_screenshot(
    screenshot: Path | str,
    settings: VisualJudgmentConfig,
) -> str:
    """Return ``fine`` or the written judgment.

    Raise ``JudgeError`` when the command is missing, exits non-zero,
    times out, or prints something other than one JSON object. Callers
    that must not crash a campaign should use ``apply_judgment``.
    """
    path = Path(screenshot)
    if settings.provider == "fake":
        return _fake_text(settings)
    if not path.is_file():
        raise JudgeError("screenshot is missing")
    return _parse_stdout(_run_command(path, settings))


def apply_judgment(
    screenshot: Path | str,
    config: CampaignConfig,
    *,
    worker_id: str,
    shard_id: str,
    backend: str,
    error: str | None = None,
    name: str = "",
    evidence: str | None = None,
    steps: list[str] | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[Finding | None, str | None]:
    """Judge one screenshot for a worker. Never raises.

    Returns ``(finding, error)``. ``finding`` is set only when the screen
    looks bad. A fine screen returns ``(None, error)``. Command failure
    returns no finding. The one-line note replaces ``error`` only when
    that field is empty.
    """
    settings = config.visual.judgment
    if not settings.enabled:
        return None, error
    try:
        text = judge_screenshot(screenshot, settings)
    except Exception as exc:
        if error and str(error).strip():
            return None, error
        return None, _fail_note(str(exc) or exc.__class__.__name__)
    if text == _FINE:
        return None, error
    return (
        _finding(
            text,
            screenshot=Path(screenshot),
            worker_id=worker_id,
            shard_id=shard_id,
            backend=backend,
            name=name,
            evidence=evidence,
            steps=steps,
            environment=environment,
        ),
        error,
    )


def _fake_text(settings: VisualJudgmentConfig) -> str:
    text = (settings.judgment or "").strip()
    if not text or text == _FINE:
        return _FINE
    return text


def _run_command(screenshot: Path, settings: VisualJudgmentConfig) -> str:
    env_name = settings.command_env or "AQA_VISUAL_JUDGE_COMMAND"
    command = (os.environ.get(env_name) or "").strip() or (settings.command or "").strip()
    if not command:
        raise JudgeError("command is unset")
    try:
        argv = shlex.split(command) + [str(screenshot)]
    except ValueError as exc:
        raise JudgeError(f"invalid command: {exc}") from exc
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise JudgeError("timed out") from exc
    except OSError as exc:
        raise JudgeError(str(exc) or "spawn failed") from exc
    if result.returncode != 0:
        raise JudgeError(f"exit {result.returncode}")
    return result.stdout or ""


def _parse_stdout(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise JudgeError("unparseable output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError("unparseable output") from exc
    if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
        raise JudgeError("unparseable output")
    if payload["ok"]:
        return _FINE
    judgment = payload.get("judgment")
    if not isinstance(judgment, str) or not judgment.strip():
        raise JudgeError("unparseable output")
    return judgment.strip()


def _finding(
    judgment: str,
    *,
    screenshot: Path,
    worker_id: str,
    shard_id: str,
    backend: str,
    name: str,
    evidence: str | None,
    steps: list[str] | None,
    environment: dict[str, str] | None,
) -> Finding:
    full = judgment.strip()
    label = name.strip() or screenshot.stem or "screen"
    shot = evidence if evidence else str(screenshot)
    digest = hashlib.sha256(str(screenshot).encode("utf-8")).hexdigest()[:8]
    return Finding(
        id=f"f-{worker_id}-{shard_id}-vj-{_slug(label)}-{digest}",
        title=_title_for(full),
        severity="medium",
        kind="visual_judgment",
        steps=list(steps) if steps else [f"judge {label}"],
        fingerprint=fingerprint_for("visual_judgment", full, label),
        worker_id=worker_id,
        backend=backend,
        shard_id=shard_id,
        screenshots=[shot],
        environment=dict(environment or {}),
        details=full,
    )


def _title_for(judgment: str) -> str:
    one_line = " ".join(judgment.split())
    if len(one_line) <= _TITLE_LIMIT:
        return one_line
    return one_line[: _TITLE_LIMIT - 3].rstrip() + "..."


def _fail_note(reason: str) -> str:
    detail = " ".join(str(reason).split()) or "command failed"
    return f"visual judgment failed open: {detail}"[:300]


def _slug(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip())
    cleaned = cleaned.strip("-") or "screen"
    return cleaned[:40]
