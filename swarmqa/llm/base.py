"""Shared behaviour for live providers.

`BaseProvider` implements the four `ModelProvider` methods once: it builds
a provider-neutral `ModelCall` (system prompt, user text, optional image,
JSON schema), checks the call and spend caps, sends it through the
transport with retries inside one deadline, prices the `Usage`, and parses
the JSON into protocol dataclasses. Adapters only translate a `ModelCall`
into their wire request and a wire response into a `Completion`.
"""

from __future__ import annotations

import random
import time
import warnings
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable

from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm import parse, schemas
from swarmqa.llm.images import PreparedImage, prepare_image
from swarmqa.llm.pricing import cost_of, price_for
from swarmqa.llm.protocol import (
    ChangedFile,
    FlowProposal,
    HistoryStep,
    ModelBudgetExceeded,
    ModelError,
    ModelTimeout,
    Rubric,
    ScreenJudgment,
    StepDecision,
    Usage,
)
from swarmqa.llm.serialize import files_text, history_text, tree_text
from swarmqa.llm.settings import LLMSettings
from swarmqa.llm.transport import TransientError, Transport, TransportTimeout
from swarmqa.llm.usage import UsageMeter

_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 20.0
_MIN_ATTEMPT_S = 0.5


def load_prompt(name: str) -> str:
    """Read `swarmqa/llm/prompts/<name>.md` (shipped as package data)."""
    return resources.files("swarmqa.llm").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8").strip()


@dataclass
class ModelCall:
    kind: str
    model: str
    system: str
    text: str
    schema_name: str
    schema: dict[str, Any]
    max_tokens: int
    effort: str
    image: PreparedImage | None = None


@dataclass
class Completion:
    """What an adapter extracts from one response. `input_tokens` excludes cache reads and writes."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class BilledError(Exception):
    """Raised by `parse_response` for a billed response that is still unusable.

    The provider records the usage, then raises `error`.
    """

    def __init__(self, completion: Completion, error: ModelError):
        super().__init__(str(error))
        self.completion = completion
        self.error = error


class BaseProvider:
    name = "base"

    def __init__(
        self,
        settings: LLMSettings,
        transport: Transport,
        *,
        meter: UsageMeter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.transport = transport
        self.meter = meter or UsageMeter()
        self._sleep = sleep
        self._clock = clock
        self._unpriced: set[str] = set()

    # Adapter hooks

    def build_request(self, call: ModelCall) -> dict[str, Any]:
        raise NotImplementedError

    def parse_response(self, raw: dict[str, Any], call: ModelCall) -> Completion:
        raise NotImplementedError

    # ModelProvider

    def decide_step(
        self,
        obs: ScreenObservation,
        goal: str,
        history: list[HistoryStep],
        *,
        timeout_s: float = 30.0,
    ) -> StepDecision:
        tree, index = tree_text(obs.tree)
        image = None
        if self.settings.step_screenshot and obs.screenshot is not None:
            image = self._image(obs)
        size = f"{obs.size[0]:.0f}x{obs.size[1]:.0f} points" if obs.size else "unknown"
        text = (
            f"Goal: {goal.strip() or '(explore the app)'}\n\n"
            f"Screen size: {size}. Frames and coordinates are in points.\n\n"
            f"Accessibility tree:\n{tree}\n\n"
            f"Earlier steps (oldest first):\n{history_text(history, limit=self.settings.history_limit)}"
        )
        call = self._call("step", load_prompt("step"), text, "step_decision", schemas.STEP_SCHEMA, image)
        data, usage = self._run(call, timeout_s)
        decision = parse.parse_step(data, index)
        decision.usage = usage
        return decision

    def judge_screen(
        self,
        obs: ScreenObservation,
        rubric: Rubric,
        *,
        timeout_s: float = 60.0,
    ) -> ScreenJudgment:
        if obs.screenshot is None:
            raise ModelError("judge_screen needs a screenshot")
        image = self._image(obs)
        tree, index = tree_text(obs.tree)
        system = rubric.instructions.strip() or load_prompt(f"judge_{rubric.name}")
        text = (
            f"The screenshot is {image.width}x{image.height} pixels; give any bbox in those pixels.\n"
            f"Only report issues you are at least {rubric.min_confidence:.2f} confident about.\n\n"
            f"Accessibility tree (frames in points, for context):\n{tree}"
        )
        call = self._call("judge", system, text, f"screen_judgment_{rubric.name}", schemas.JUDGE_SCHEMA, image)
        data, usage = self._run(call, timeout_s)
        judgment = parse.parse_judgment(
            data, index, min_confidence=rubric.min_confidence, to_points=image.to_points
        )
        judgment.usage = usage
        return judgment

    def propose_flows(
        self,
        diff: str,
        files: list[ChangedFile],
        *,
        max_flows: int = 10,
        timeout_s: float = 120.0,
    ) -> FlowProposal:
        text = (
            f"Propose at most {max_flows} flows.\n\n"
            + files_text(diff, files, max_chars=self.settings.max_prompt_chars)
        )
        call = self._call("flows", load_prompt("flows"), text, "flow_proposal", schemas.FLOWS_SCHEMA, None)
        data, usage = self._run(call, timeout_s)
        proposal = parse.parse_flows(data, max_flows=max_flows)
        proposal.usage = usage
        return proposal

    def computer_use(
        self,
        obs: ScreenObservation,
        instruction: str,
        *,
        timeout_s: float = 60.0,
    ) -> StepDecision:
        if obs.screenshot is None:
            raise ModelError("computer_use needs a screenshot")
        image = self._image(obs)
        text = (
            f"Instruction: {instruction.strip()}\n\n"
            f"The screenshot is {image.width}x{image.height} pixels. "
            "Give x, y, end_x and end_y in those pixels, origin top-left."
        )
        call = self._call(
            "computer_use", load_prompt("computer_use"), text, "computer_action", schemas.COMPUTER_USE_SCHEMA, image
        )
        data, usage = self._run(call, timeout_s)
        decision = parse.parse_computer_action(data, to_points=image.to_points)
        decision.usage = usage
        return decision

    # Internals

    def _image(self, obs: ScreenObservation) -> PreparedImage:
        assert obs.screenshot is not None
        return prepare_image(obs.screenshot, max_edge=self.settings.max_image_edge, scale=obs.scale or 1.0)

    def _call(
        self,
        kind: str,
        system: str,
        text: str,
        schema_name: str,
        schema: dict[str, Any],
        image: PreparedImage | None,
    ) -> ModelCall:
        return ModelCall(
            kind=kind,
            model=self.settings.model_for(kind),
            system=system,
            text=text,
            schema_name=schema_name,
            schema=schema,
            max_tokens=self.settings.max_tokens_for(kind),
            effort=self.settings.effort_for(kind),
            image=image,
        )

    def _check_budget(self, call: ModelCall) -> None:
        settings = self.settings
        # The spend cap is soft: concurrent calls each see spend so far plus
        # their own estimate, so in-flight calls can pass it by their cost.
        if settings.max_cost is not None:
            estimate = self._estimate_cost(call)
            if self.meter.cost + estimate > settings.max_cost + 1e-9:
                raise ModelBudgetExceeded(
                    f"spend cap {settings.max_cost:.4f} {settings.currency} would be passed "
                    f"(spent {self.meter.cost:.4f}, next call about {estimate:.4f})"
                )

    def _estimate_cost(self, call: ModelCall) -> float:
        tokens = (len(call.system) + len(call.text)) // 4
        if call.image is not None:
            tokens += call.image.estimated_tokens
        return cost_of(price_for(call.model, self.settings.prices), input_tokens=tokens)

    def _run(self, call: ModelCall, timeout_s: float) -> tuple[dict[str, Any], Usage]:
        self._check_budget(call)
        request = self.build_request(call)
        self.meter.start_call(self.settings.max_calls)
        timeout = self.settings.call_timeout(timeout_s)
        started = self._clock()
        deadline = started + timeout
        attempt = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ModelTimeout(f"{call.kind} timed out after {timeout:.1f}s")
            try:
                raw = self.transport.send(request, timeout_s=remaining)
                break
            except TransportTimeout as exc:
                failure: Exception = exc
                retry_after = None
            except TransientError as exc:
                failure = exc
                retry_after = exc.retry_after
            if attempt >= self.settings.max_retries:
                if isinstance(failure, TransportTimeout):
                    raise ModelTimeout(f"{call.kind} timed out after {attempt + 1} attempts") from failure
                raise ModelError(f"{call.kind} failed after {attempt + 1} attempts: {failure}") from failure
            delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2**attempt)) + random.uniform(0, 0.25)
            if retry_after is not None:
                delay = max(delay, retry_after)
            remaining = deadline - self._clock()
            if delay + _MIN_ATTEMPT_S > remaining:
                if isinstance(failure, TransportTimeout):
                    raise ModelTimeout(f"{call.kind} timed out after {timeout:.1f}s") from failure
                raise ModelTimeout(f"{call.kind} ran out of time retrying: {failure}") from failure
            self._sleep(delay)
            attempt += 1
        latency = self._clock() - started
        try:
            completion = self.parse_response(raw, call)
        except BilledError as billed:
            self.meter.add(self._usage(billed.completion, latency))
            raise billed.error from None
        usage = self._usage(completion, latency)
        self.meter.add(usage)
        return parse.load_json(completion.text), usage

    def _usage(self, completion: Completion, latency: float) -> Usage:
        price = price_for(completion.model, self.settings.prices)
        if price is None and completion.model not in self._unpriced:
            self._unpriced.add(completion.model)
            warnings.warn(
                f"no price for model {completion.model!r}; its cost counts as 0 (set llm prices)",
                RuntimeWarning,
                stacklevel=3,
            )
        return Usage(
            provider=self.name,
            model=completion.model,
            input_tokens=completion.input_tokens + completion.cache_write_tokens,
            output_tokens=completion.output_tokens,
            cache_read_tokens=completion.cache_read_tokens,
            cost=cost_of(
                price,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cache_read_tokens=completion.cache_read_tokens,
                cache_write_tokens=completion.cache_write_tokens,
            ),
            currency=self.settings.currency,
            latency_s=round(latency, 3),
        )


def int_field(mapping: Any, key: str) -> int:
    """Read a token count that may be missing or null."""
    if not isinstance(mapping, dict):
        return 0
    value = mapping.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
