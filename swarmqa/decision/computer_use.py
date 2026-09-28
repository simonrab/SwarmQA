"""Computer-use decision backend — shell-out JSON command or fake provider."""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
from typing import Any

from swarmqa.decision.heuristic import query_for
from swarmqa.decision.protocol import DecisionAction, Observation
from swarmqa.decision.providers.fake import FakeComputerUseProvider
from swarmqa.decision.serialize_tree import summarize_tree
from swarmqa.driver.query import walk
from swarmqa.models import ComputerUseConfig, DecisionConfig, ElementQuery, UIElement


class ComputerUseError(Exception):
    """Raised when computer-use cannot produce a usable action."""


def map_point_to_element(
    elements: list[UIElement], x: float, y: float
) -> UIElement | None:
    """Return the element whose frame center is nearest to ``(x, y)``.

    Elements without a 4-tuple ``frame`` of ``(x, y, width, height)`` are
    skipped. Returns ``None`` when no framed element exists.
    """
    best: UIElement | None = None
    best_dist = math.inf
    for element in walk(elements):
        frame = element.frame
        if frame is None or len(frame) != 4:
            continue
        fx, fy, width, height = frame
        cx = fx + width / 2.0
        cy = fy + height / 2.0
        dist = (cx - x) ** 2 + (cy - y) ** 2
        if dist < best_dist:
            best_dist = dist
            best = element
    return best


def action_from_payload(
    payload: dict[str, Any], elements: list[UIElement]
) -> DecisionAction:
    """Map a computer-use JSON object onto a ``DecisionAction``."""
    kind = payload.get("action") or payload.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ComputerUseError("missing action")
    kind = kind.lower()

    if kind == "click":
        if "x" in payload and "y" in payload:
            try:
                x = float(payload["x"])
                y = float(payload["y"])
            except (TypeError, ValueError) as exc:
                raise ComputerUseError("invalid click coordinates") from exc
            element = map_point_to_element(elements, x, y)
            if element is None:
                raise ComputerUseError("no element near coordinates")
            label = element.label or element.identifier or element.role
            return DecisionAction(
                kind="click",
                query=query_for(element),
                label=label,
                rationale=f"computer_use click at ({x:g},{y:g}) → {label}",
            )
        label = payload.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ComputerUseError("click requires x/y or label")
        label = label.strip()
        return DecisionAction(
            kind="click",
            query=ElementQuery(label=label),
            label=label,
            rationale=f"computer_use click label={label}",
        )

    if kind == "menu":
        path = payload.get("path") or payload.get("menu_path")
        if not isinstance(path, list) or not path or not all(
            isinstance(part, str) for part in path
        ):
            raise ComputerUseError("menu requires path list of strings")
        joined = " > ".join(path)
        return DecisionAction(
            kind="menu",
            menu_path=list(path),
            label=joined,
            rationale=f"computer_use menu: {joined}",
        )

    if kind in ("done", "noop", "missing"):
        label = payload.get("label")
        return DecisionAction(
            kind=kind,  # type: ignore[arg-type]
            label=label if isinstance(label, str) else None,
            rationale=f"computer_use {kind}",
        )

    raise ComputerUseError(f"unsupported action: {kind}")


class ComputerUseEvaluator:
    """Budgeted computer-use stage: shell-out JSON command or fake provider.

    Command comes from the env var named by ``computer_use.command_env``
    (default ``AQA_COMPUTER_USE_COMMAND``). Screenshot path is passed as the
    final argv token; optional a11y hint JSON is written to stdin when
    ``include_a11y_hint`` is true.

    Caps: ``computer_use.max_calls`` and the shared ``decision.max_model_calls``
    budget (tracked via ``note_model_call`` / remaining checks from cascade).
    """

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        computer_use: ComputerUseConfig | None = None,
        timeout_s: float | None = None,
        provider: FakeComputerUseProvider | None = None,
        model_calls_used: int = 0,
    ):
        self.config = config or DecisionConfig()
        self.computer_use = computer_use or self.config.computer_use
        self.timeout_s = (
            self.config.model_timeout_s if timeout_s is None else timeout_s
        )
        self._calls = 0
        self._model_calls_used = model_calls_used
        if provider is not None:
            self._fake = provider
        elif self.computer_use.provider == "fake":
            self._fake = FakeComputerUseProvider()
        else:
            self._fake = None

    @property
    def calls(self) -> int:
        return self._calls

    def note_model_call(self) -> None:
        """Record an external model call against the shared campaign budget."""
        self._model_calls_used += 1

    def set_model_calls_used(self, used: int) -> None:
        self._model_calls_used = used

    def remaining_calls(self) -> int:
        cu_left = self.computer_use.max_calls - self._calls
        model_left = self.config.max_model_calls - self._model_calls_used
        return max(0, min(cu_left, model_left))

    def decide(self, obs: Observation) -> DecisionAction:
        if self.remaining_calls() <= 0:
            raise ComputerUseError("call budget exhausted")

        screenshot = obs.screenshot_path
        hint = self._hint(obs) if self.computer_use.include_a11y_hint else None

        if self._fake is not None:
            payload = self._fake.run(
                screenshot_path=screenshot, hint=hint, obs=obs
            )
        else:
            if not screenshot:
                raise ComputerUseError("screenshot_path is required")
            payload = self._run_command(screenshot, hint)

        if not isinstance(payload, dict):
            raise ComputerUseError("response must be an object")

        action = action_from_payload(payload, obs.elements)
        self._calls += 1
        self._model_calls_used += 1
        return action

    def _hint(self, obs: Observation) -> dict[str, Any]:
        tree = obs.tree_summary or summarize_tree(obs.elements)
        return {
            "goal": obs.goal,
            "tokens": list(obs.tokens),
            "expected": list(obs.expected),
            "tree": tree,
            "stall_count": obs.stall_count,
        }

    def _run_command(
        self, screenshot_path: str, hint: dict[str, Any] | None
    ) -> dict[str, Any]:
        env_name = self.computer_use.command_env
        command = (os.environ.get(env_name) or "").strip()
        if not command:
            raise ComputerUseError(f"command env {env_name} is unset")
        try:
            argv = shlex.split(command) + [screenshot_path]
        except ValueError as exc:
            raise ComputerUseError(f"invalid command: {exc}") from exc
        stdin = json.dumps(hint) if hint is not None else None
        try:
            result = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ComputerUseError("timed out") from exc
        except OSError as exc:
            raise ComputerUseError(str(exc) or "spawn failed") from exc
        if result.returncode != 0:
            raise ComputerUseError(f"exit {result.returncode}")
        raw = (result.stdout or "").strip()
        if not raw:
            raise ComputerUseError("empty stdout")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ComputerUseError("invalid JSON response") from exc
        if not isinstance(parsed, dict):
            raise ComputerUseError("response must be an object")
        return parsed
