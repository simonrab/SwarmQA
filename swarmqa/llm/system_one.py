"""System One provider: typed questions in, calibrated probabilities out.

Speaks the `POST /v1/systemone` contract that TypeSafe's Jev and the open
Jev-compatible servers (Laya, kev, decider, OpenJev) share:

    {"model": "jev-latest", "state": "...",
     "questions": {"next": {"type": "choice", "instructions": "...",
                            "criteria": {"o1": "button \\"Save\\"", "back": "..."}}}}
    -> {"model": "jev-1.13.0",
        "answers": {"next": {"choice": "o1", "probabilities": {...}, "confidence": 0.91}},
        "usage": {"input_tokens": 812, "output_tokens": 0}}

`decide_step` asks the model to choose among the screen's actionable controls
(plus `back` and `done`), so it can only answer with something the loop can
do. When the answer's confidence is below `min_confidence` the step goes to
the escalation provider (`escalate_to`), or raises `ModelRefused` so the
agent loop falls back to its rules. `triage` asks whether a finding is a real
bug or an artifact of the simulator or of SwarmQA itself.

Stdlib only. Settings come from the `[llm.system_one]` table.
"""

from __future__ import annotations

import json
import math
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping

from swarmqa.checks._tree import walk
from swarmqa.explorer.screen_graph import Control, actionable_controls, fingerprint, modal_scope, normalize_role
from swarmqa.llm.protocol import (
    ChangedFile,
    FlowProposal,
    HistoryStep,
    ModelError,
    ModelRefused,
    ModelTimeout,
    Rubric,
    ScreenJudgment,
    StepDecision,
    Usage,
)
from swarmqa.llm.serialize import NO_EFFECT, describe_action, history_text, tree_text
from swarmqa.llm.transport import TransientError, Transport, TransportTimeout
from swarmqa.llm.usage import UsageMeter

if TYPE_CHECKING:
    from swarmqa.driver.protocol import ScreenObservation
    from swarmqa.llm.protocol import ModelProvider
    from swarmqa.models import Finding

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# TypeSafe's published input price; output tokens are free.
TYPESAFE_PRICE_PER_MTOK = 0.042

VERDICTS: dict[str, str] = {
    "real_bug": "A real defect in the app that a user of the app could hit.",
    "environment_artifact": (
        "Caused by the simulator or test environment (no network, no backend, missing "
        "permissions, simulator-only service errors), not by the app's own code."
    ),
    "harness_artifact": (
        "Caused by the testing tool itself (it tapped the wrong place, typed into the "
        "wrong field, misread the screen, or a check that is too strict)."
    ),
    "expected_behavior": "The app is behaving as designed; nothing is wrong.",
}
ARTIFACT_VERDICTS = ("environment_artifact", "harness_artifact")
# Roles whose label is screen text rather than a control (for the compact state).
_TEXT_ROLES = {"text", "statictext", "navigationbar", "header", "heading"}

_RETRYABLE_STATUS = {408, 409, 429}
_BACKOFF_BASE_S = 0.25
_MIN_ATTEMPT_S = 0.2


@dataclass
class SystemOneSettings:
    """The `[llm.system_one]` table.

    `base_url` is the server root (`/v1/systemone` is appended). `model` is
    sent as given (`jev-latest` for TypeSafe; local servers ignore it or
    pick their own). `api_key_env` names the bearer token's variable; unset
    or empty sends no header. `min_confidence` gates every answer;
    A `done` answer ends the goal only with `trust_done` and at
    `done_min_confidence` or above, since a wrong `done` stops the whole
    goal. Without `trust_done` it counts as unsure: the escalation provider
    decides, or the rules carry on (Laya called `done` early on every
    PlantedBugs goal, once at 0.84).
    `escalate_to` names an `[llm]` provider for unsure steps and for screen
    judging. `max_choices` caps the controls offered per step (Jev takes up
    to 255; small open models get worse past a few dozen). `max_state_chars`
    bounds the screen text sent. `state_format` is `compact` (the goal, the
    screen's visible text and the last few steps; small encoder models such as
    Laya lose the goal in a long state) or `tree` (the numbered accessibility
    tree, for Jev and other long-context models). `triage` turns finding
    triage on.
    `price_per_mtok` overrides the input price (0 for local servers).
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = "jev-latest"
    api_key_env: str = "TYPESAFE_API_KEY"
    min_confidence: float = 0.5
    done_min_confidence: float = 0.8
    trust_done: bool = False
    escalate_to: str = ""
    max_choices: int = 40
    max_state_chars: int = 6000
    state_format: str = "compact"
    history_limit: int = 8
    sample_text: str = "test"
    triage: bool = True
    price_per_mtok: float | None = None

    def __post_init__(self) -> None:
        for name in ("min_confidence", "done_min_confidence"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"system_one.{name} must be in 0..1")
        if not 2 <= self.max_choices <= 255:
            raise ValueError("system_one.max_choices must be in 2..255")
        if self.state_format not in ("compact", "tree"):
            raise ValueError("system_one.state_format must be 'compact' or 'tree'")
        if self.escalate_to in ("system_one", "chat"):
            raise ValueError(f"system_one.escalate_to cannot be {self.escalate_to!r}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SystemOneSettings":
        raw = dict(raw or {})
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown llm.system_one settings: {', '.join(unknown)}")
        return cls(**raw)

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/v1/systemone") else f"{base}/v1/systemone"

    def price(self) -> float:
        if self.price_per_mtok is not None:
            return self.price_per_mtok
        return TYPESAFE_PRICE_PER_MTOK if "typesafe.ai" in self.base_url else 0.0


# Answers ------------------------------------------------------------------


@dataclass
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class SystemOneResult:
    model: str
    answers: dict[str, dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0

    def choice(self, question: str, allowed: list[str]) -> ChoiceAnswer:
        raw = self.answers.get(question)
        if not isinstance(raw, dict):
            raise ModelRefused(f"no answer for {question!r}")
        probabilities = _probabilities(raw.get("probabilities"))
        choice = raw.get("choice")
        if not isinstance(choice, str) and probabilities:
            choice = max(probabilities, key=probabilities.__getitem__)
        if not isinstance(choice, str) or choice not in allowed:
            raise ModelRefused(f"{question}: choice {choice!r} is not one of the options")
        confidence = raw.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            value = float(confidence)
        else:
            value = normalized_confidence(probabilities, len(allowed))
        return ChoiceAnswer(choice=choice, probabilities=probabilities, confidence=min(1.0, max(0.0, value)))

    def noul(self, question: str) -> float | None:
        raw = self.answers.get(question)
        if isinstance(raw, dict):
            raw = raw.get("noul", raw.get("probability"))
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return min(1.0, max(0.0, float(raw)))
        return None


def _probabilities(raw: Any) -> dict[str, float]:
    """`{"a": 0.9}` (Jev) or `[{"value": "a", "probability": 0.9}]` (OpenJev decisions)."""
    out: dict[str, float] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((row.get("value"), row.get("probability")) for row in raw if isinstance(row, dict))
    else:
        return out
    for key, value in items:
        if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    return out


def normalized_confidence(probabilities: dict[str, float], k: int) -> float:
    """Jev's confidence, 1 - H(p) / ln K: 1 when certain, 0 when uniform."""
    if not probabilities or k < 2:
        return 0.0
    entropy = -sum(p * math.log(p) for p in probabilities.values() if p > 0)
    return 1.0 - entropy / math.log(k)


# Transport ----------------------------------------------------------------


@dataclass
class HTTPTransport:
    """POST the request as JSON to `url` with stdlib urllib."""

    url: str
    api_key: str | None = None

    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(request).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
            message = f"HTTP {exc.code}: {body}".strip()
            if exc.code in _RETRYABLE_STATUS or exc.code >= 500:
                retry_after = exc.headers.get("retry-after") if exc.headers else None
                raise TransientError(message, status=exc.code, retry_after=_float(retry_after)) from exc
            raise ModelError(message) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TransportTimeout(f"timed out after {timeout_s:.1f}s") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeout(f"timed out after {timeout_s:.1f}s") from exc
            raise TransientError(f"cannot reach {self.url}: {exc.reason}") from exc
        except OSError as exc:
            raise TransientError(f"cannot reach {self.url}: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelRefused("System One response is not JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelRefused("System One response is not an object")
        return parsed


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Client -------------------------------------------------------------------


class SystemOneClient:
    """One `/v1/systemone` call with retries inside a deadline."""

    def __init__(
        self,
        settings: SystemOneSettings,
        transport: Transport,
        *,
        max_retries: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.transport = transport
        self.max_retries = max_retries
        self._clock = clock
        self._sleep = sleep

    def ask(self, state: str | dict[str, Any], questions: dict[str, dict[str, Any]], *, timeout_s: float) -> SystemOneResult:
        request: dict[str, Any] = {"model": self.settings.model, "state": state, "questions": questions}
        started = self._clock()
        deadline = started + timeout_s
        attempt = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ModelTimeout(f"system_one timed out after {timeout_s:.1f}s")
            try:
                raw = self.transport.send(request, timeout_s=remaining)
                break
            except TransportTimeout as exc:
                failure: Exception = exc
                retry_after = None
            except TransientError as exc:
                failure = exc
                retry_after = exc.retry_after
            if attempt >= self.max_retries:
                if isinstance(failure, TransportTimeout):
                    raise ModelTimeout(f"system_one timed out after {attempt + 1} attempts") from failure
                raise ModelError(f"system_one failed after {attempt + 1} attempts: {failure}") from failure
            delay = _BACKOFF_BASE_S * (2**attempt) + random.uniform(0, 0.1)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if delay + _MIN_ATTEMPT_S > deadline - self._clock():
                raise ModelTimeout(f"system_one ran out of time retrying: {failure}") from failure
            self._sleep(delay)
            attempt += 1
        answers = raw.get("answers")
        if not isinstance(answers, dict):
            raise ModelRefused("System One response has no answers")
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return SystemOneResult(
            model=str(raw.get("model") or self.settings.model),
            answers=answers,
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            latency_s=self._clock() - started,
        )


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


# Provider -----------------------------------------------------------------


@dataclass
class Triage:
    verdict: str
    confidence: float
    user_visible: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    usage: Usage | None = None

    @property
    def artifact(self) -> bool:
        return self.verdict in ARTIFACT_VERDICTS


@dataclass
class SystemOneStats:
    """Counters for reports and benchmarks."""

    calls: int = 0
    steps: int = 0
    confident: int = 0
    escalated: int = 0
    refused: int = 0
    errors: int = 0
    latencies_s: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies_s)
        p50 = lat[len(lat) // 2] if lat else None
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
        return {
            "calls": self.calls,
            "steps": self.steps,
            "confident": self.confident,
            "escalated": self.escalated,
            "refused": self.refused,
            "errors": self.errors,
            "latency_p50_s": p50,
            "latency_p95_s": p95,
        }


_STEP_INSTRUCTIONS = (
    "You are testing an iOS app. Pick the one action that moves the app closest to the goal. "
    "Prefer controls whose name matches the goal. Do not pick an action the recent steps show had "
    "no effect on this screen. Pick 'done' only if the goal is already visibly met on this screen; "
    "pick 'back' if this screen cannot lead to the goal."
)


class SystemOneProvider:
    """`ModelProvider` backed by a System One server, with an optional LLM to escalate to."""

    name = "system_one"

    def __init__(
        self,
        settings: SystemOneSettings,
        transport: Transport,
        *,
        escalate: "ModelProvider | None" = None,
        meter: UsageMeter | None = None,
        max_retries: int = 2,
        max_calls: int | None = None,
        escalate_timeout_s: float | None = None,
    ):
        self.settings = settings
        self.escalate_timeout_s = escalate_timeout_s
        self.client = SystemOneClient(settings, transport, max_retries=max_retries)
        self.escalate = escalate
        self.meter = meter or UsageMeter()
        self.max_calls = max_calls
        self.stats = SystemOneStats()

    # Steps ----------------------------------------------------------------

    def decide_step(
        self,
        obs: "ScreenObservation",
        goal: str,
        history: list[HistoryStep],
        *,
        timeout_s: float = 30.0,
    ) -> StepDecision:
        self.stats.steps += 1
        options = self.step_options(obs, history)
        criteria = {key: text for key, (text, _) in options.items()}
        try:
            result = self._ask(
                self.step_state(obs, goal, history),
                {"next_step": {"type": "choice", "instructions": _STEP_INSTRUCTIONS, "criteria": criteria}},
                timeout_s=timeout_s,
            )
            answer = result.choice("next_step", list(criteria))
        except ModelError as exc:
            self.stats.errors += 1
            return self._unsure(obs, goal, history, timeout_s, f"system_one failed ({exc})", exc)
        usage = self._usage(result)
        top = answer.probabilities.get(answer.choice)
        note = f"system_one {answer.choice} confidence={answer.confidence:.2f}" + (
            f" p={top:.2f}" if top is not None else ""
        )
        bar = self.settings.min_confidence
        if answer.choice == "done":
            if not self.settings.trust_done:
                return self._unsure(obs, goal, history, timeout_s, f"{note} needs confirming", None)
            bar = max(bar, self.settings.done_min_confidence)
        if answer.confidence < bar:
            return self._unsure(obs, goal, history, timeout_s, f"{note} below {bar}", None)
        self.stats.confident += 1
        decision = options[answer.choice][1]
        return replace(
            decision,
            rationale=f"{note}: {criteria[answer.choice]}",
            confidence=answer.confidence,
            usage=usage,
        )

    def _unsure(
        self,
        obs: "ScreenObservation",
        goal: str,
        history: list[HistoryStep],
        timeout_s: float,
        why: str,
        cause: ModelError | None,
    ) -> StepDecision:
        if self.escalate is None:
            self.stats.refused += 1
            raise ModelRefused(f"{why}; no escalation provider") from cause
        self.stats.escalated += 1
        # The loop's step timeout is sized for System One; the escalation
        # provider (a CLI agent, say) keeps its own, longer one.
        budget = max(timeout_s, self.escalate_timeout_s or 0.0)
        decision = self.escalate.decide_step(obs, goal, history, timeout_s=budget)
        decision.rationale = f"{why}; escalated to {self.escalate.name}: {decision.rationale}"
        return decision

    def step_options(
        self, obs: "ScreenObservation", history: list[HistoryStep]
    ) -> dict[str, tuple[str, StepDecision]]:
        """Option id -> (description, decision). Controls with no effect on this screen are left out."""
        screen = fingerprint(obs.tree)
        dead = {
            _decision_key(step.action)
            for step in history
            if step.outcome == NO_EFFECT and step.screen == screen
        }
        options: dict[str, tuple[str, StepDecision]] = {}
        for control in actionable_controls(obs.tree):
            if len(options) >= self.settings.max_choices - 2:
                break
            decision = self._control_decision(control)
            if _decision_key(decision) in dead:
                continue
            options[f"o{len(options) + 1}"] = (_describe(control), decision)
        options["back"] = ("Go back to the previous screen.", StepDecision(kind="back"))
        options["done"] = ("The goal is already met on this screen.", StepDecision(kind="done"))
        return options

    def _control_decision(self, control: Control) -> StepDecision:
        if control.kind == "type":
            return StepDecision(kind="type", target=control.query(), text=self.settings.sample_text, point=control.point)
        if control.point is not None:
            return StepDecision(kind="tap_point", point=control.point)
        return StepDecision(kind="tap", target=control.query())

    def step_state(self, obs: "ScreenObservation", goal: str, history: list[HistoryStep]) -> str:
        goal = goal or "explore the app and find bugs"
        if self.settings.state_format == "compact":
            return compact_state(obs, goal, history, max_chars=self.settings.max_state_chars)
        screen, _ = tree_text(obs.tree)
        limit = self.settings.max_state_chars
        if len(screen) > limit:
            screen = screen[:limit] + "\n[screen text truncated]"
        recent = history_text(history, limit=self.settings.history_limit)
        return f"Goal: {goal}\n\nScreen:\n{screen}\n\nRecent steps:\n{recent}"

    # Triage ---------------------------------------------------------------

    def triage(self, finding: "Finding", *, timeout_s: float = 10.0) -> Triage:
        """Is `finding` a real bug, an artifact, or expected behaviour?"""
        state = triage_state(finding)
        result = self._ask(
            state,
            {
                "verdict": {
                    "type": "choice",
                    "instructions": "An automated iOS app tester reported this finding. What is it?",
                    "criteria": dict(VERDICTS),
                },
                "user_visible": {
                    "type": "noul",
                    "instructions": "A person using the app on a real phone would notice this problem.",
                },
            },
            timeout_s=timeout_s,
        )
        answer = result.choice("verdict", list(VERDICTS))
        return Triage(
            verdict=answer.choice,
            confidence=answer.confidence,
            user_visible=result.noul("user_visible"),
            probabilities=answer.probabilities,
            usage=self._usage(result),
        )

    # Other calls ----------------------------------------------------------

    def judge_screen(self, obs: "ScreenObservation", rubric: Rubric, *, timeout_s: float = 60.0) -> ScreenJudgment:
        if self.escalate is None:
            raise ModelError("system_one does not judge screens; set system_one.escalate_to")
        return self.escalate.judge_screen(obs, rubric, timeout_s=timeout_s)

    def propose_flows(
        self, diff: str, files: list[ChangedFile], *, max_flows: int = 10, timeout_s: float = 120.0
    ) -> FlowProposal:
        if self.escalate is None:
            raise ModelError("system_one does not propose flows; set system_one.escalate_to")
        return self.escalate.propose_flows(diff, files, max_flows=max_flows, timeout_s=timeout_s)

    def computer_use(self, obs: "ScreenObservation", instruction: str, *, timeout_s: float = 60.0) -> StepDecision:
        if self.escalate is None:
            raise ModelError("system_one does not do computer use; set system_one.escalate_to")
        return self.escalate.computer_use(obs, instruction, timeout_s=timeout_s)

    # Internals ------------------------------------------------------------

    def _ask(self, state: str, questions: dict[str, dict[str, Any]], *, timeout_s: float) -> SystemOneResult:
        self.meter.start_call(self.max_calls)
        self.stats.calls += 1
        result = self.client.ask(state, questions, timeout_s=timeout_s)
        self.stats.latencies_s.append(result.latency_s)
        return result

    def _usage(self, result: SystemOneResult) -> Usage:
        usage = Usage(
            provider=self.name,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost=result.input_tokens * self.settings.price() / 1_000_000,
            latency_s=result.latency_s,
        )
        self.meter.add(usage)
        return usage


def compact_state(obs: "ScreenObservation", goal: str, history: list[HistoryStep], *, max_chars: int) -> str:
    """The goal first, then the screen's text, then the last three steps, in a few hundred characters."""
    texts: list[str] = []
    for element in walk(modal_scope(obs.tree)):
        role = normalize_role(element.role)
        text = (element.label or element.value or "").strip()
        if role in _TEXT_ROLES and text and text not in texts:
            texts.append(text[:80])
    screen = "; ".join(texts)[: max(200, max_chars // 4)] or "(no text)"
    lines = [f"Goal: {goal}", f"Screen shows: {screen}"]
    if history:
        steps = [
            describe_action(step.action) + (" (no effect)" if step.outcome == NO_EFFECT else "")
            for step in history[-3:]
        ]
        lines.append("Last steps: " + "; ".join(steps))
    return "\n".join(lines)[:max_chars]


def _describe(control: Control) -> str:
    verb = "Type into" if control.kind == "type" else "Tap"
    text = f'{verb} {control.role} "{control.display()[:80]}"'
    if control.identifier and control.identifier != control.label:
        text += f" (id {control.identifier})"
    return text


def _decision_key(decision: StepDecision) -> str:
    target = decision.target
    if target is not None:
        return f"{decision.kind}|{target.role}|{target.label or ''}|{target.identifier or ''}"
    if decision.point is not None:
        return f"{decision.kind}|{round(decision.point[0])},{round(decision.point[1])}"
    return decision.kind


def triage_state(finding: "Finding") -> str:
    lines = [
        f"Title: {finding.title}",
        f"Kind: {finding.kind} (category {finding.category}, severity {finding.severity})",
        f"Reported by: {'a model only' if finding.advisory else 'an automatic check'}",
    ]
    check = finding.environment.get("check")
    if check:
        lines.append(f"Check: {check}")
    if finding.details:
        lines.append(f"Details: {finding.details[:1500]}")
    if finding.steps:
        lines.append("Steps:\n" + "\n".join(f"- {step}" for step in finding.steps[-12:]))
    env = {k: v for k, v in finding.environment.items() if k in ("platform", "device", "os", "backend")}
    if env:
        lines.append("Environment: iOS Simulator; " + ", ".join(f"{k}={v}" for k, v in sorted(env.items())))
    else:
        lines.append("Environment: iOS Simulator")
    return "\n".join(lines)


def apply_triage(provider: Any, findings: list["Finding"], *, min_confidence: float) -> list[tuple["Finding", Triage]]:
    """Triage each finding with `provider.triage`; failures leave the finding as it was.

    A confident artifact verdict makes the finding advisory (it is kept, not
    dropped). The verdict is recorded in `environment` and `details`.
    """
    done: list[tuple[Finding, Triage]] = []
    for finding in findings:
        try:
            result = provider.triage(finding)
        except ModelError:
            continue
        finding.environment["system_one_verdict"] = result.verdict
        finding.environment["system_one_confidence"] = f"{result.confidence:.2f}"
        if result.user_visible is not None:
            finding.environment["system_one_user_visible"] = f"{result.user_visible:.2f}"
        note = f"System One triage: {result.verdict} (confidence {result.confidence:.2f})"
        if result.artifact and result.confidence >= min_confidence:
            finding.advisory = True
            finding.confidence = min(finding.confidence, 1.0 - result.confidence)
            note += "; marked advisory"
        finding.details = f"{finding.details}\n\n{note}".strip()
        done.append((finding, result))
    return done


def create_system_one_provider(
    raw: Mapping[str, Any] | None,
    *,
    escalate: "ModelProvider | None" = None,
    transport: Transport | None = None,
    meter: UsageMeter | None = None,
    max_retries: int = 2,
    max_calls: int | None = None,
    escalate_timeout_s: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> SystemOneProvider:
    import os

    settings = SystemOneSettings.from_mapping(raw)
    env = os.environ if environ is None else environ
    key = (env.get(settings.api_key_env) or "").strip() if settings.api_key_env else ""
    live = transport or HTTPTransport(settings.endpoint(), api_key=key or None)
    return SystemOneProvider(
        settings,
        live,
        escalate=escalate,
        meter=meter,
        max_retries=max_retries,
        max_calls=max_calls,
        escalate_timeout_s=escalate_timeout_s,
    )
