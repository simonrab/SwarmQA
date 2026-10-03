"""WP-D2: self-heal. Re-explore one cached step that no longer matches the app.

When a cached scripted shard fails on a step because its element is gone
(a renamed button, a moved control), `heal_flow` runs the agent loop in goal
mode from the screen the app is on, capped at `flows.heal_max_steps`. The
model, when there is one, sees the original goal plus a hint naming the
failed step; the heuristic sees the goal alone. If the loop reaches the
goal, its steps replace the failed step and everything after it, and the
flow is saved with `heals + 1`. If it does not, the entry is dropped so the
next run explores from scratch, and the scripted result stands.

A crash, a timeout or a failed launch is a finding, never a heal.

`Healer` is the `on_step_failed` hook for `run_scripted`. See docs/flows.md.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from swarmqa.errors import ElementNotFoundError, IntentError
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.explorer.scripted import StepFailure
from swarmqa.flows.cache import CachedFlow, FlowCache, cache_root, clean_steps, is_cached_shard, shard_key
from swarmqa.flows.settings import FlowsSettings
from swarmqa.llm.protocol import ModelProvider
from swarmqa.models import CampaignConfig, Shard, WorkerResult


def healable(error: BaseException) -> bool:
    """A step failure the cache may repair: the step's element is not on screen."""
    return isinstance(error, ElementNotFoundError)


def heal_hint(index: int, description: str, error: BaseException) -> str:
    return (
        f"Hint: a recorded script for this goal stopped at step {index + 1} ({description}): {error}. "
        "The app has changed. Reach the goal from the current screen."
    )


class _LiveDriver:
    """The scripted run's driver, already launched and on the failing screen.

    `launch` is a no-op so the loop starts where the script stopped; video
    and closing stay with the scripted run.
    """

    def __init__(self, driver: Any):
        self._driver = driver

    def __getattr__(self, name: str):
        if name == "_driver":
            raise AttributeError(name)
        return getattr(self._driver, name)

    def launch(self) -> None:
        return None

    def start_video(self) -> None:
        return None

    def stop_video(self) -> None:
        return None

    def close(self) -> None:
        return None


class _HintedProvider:
    """Adds the heal hint to the goal the model sees."""

    def __init__(self, provider: ModelProvider, hint: str):
        self._provider = provider
        self._hint = hint

    def __getattr__(self, name: str):
        if name == "_provider":
            raise AttributeError(name)
        return getattr(self._provider, name)

    def decide_step(self, obs, goal, history, *, timeout_s: float = 30.0):
        return self._provider.decide_step(obs, f"{goal}\n\n{self._hint}", history, timeout_s=timeout_s)


@dataclass
class HealResult:
    """How a heal ended. `flow` is the rewritten flow when `healed`."""

    healed: bool
    reason: str
    flow: CachedFlow | None = None
    steps: list[dict] = field(default_factory=list)
    dropped: bool = False
    result: WorkerResult | None = None


def heal_flow(
    flow: CachedFlow,
    failure: StepFailure,
    config: CampaignConfig,
    *,
    cache: FlowCache,
    work_dir: Path | str,
    settings: FlowsSettings | None = None,
    provider: ModelProvider | None = None,
    driver: Any = None,
    worker_id: str = "heal",
    loop_settings: AgentLoopSettings | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> HealResult:
    """Re-explore `failure`'s step of `flow` from the current screen and rewrite the flow.

    `driver` defaults to `failure.driver`. The loop's files go to
    `work_dir`, never to the campaign's findings. On success the flow keeps
    `steps[:failure.index]`, gets the loop's steps after them, and is saved
    with `heals + 1`; otherwise the entry is dropped (`dropped=True`), unless
    the failure was not one a heal may repair, which leaves the cache alone.
    """
    settings = settings or FlowsSettings.from_config(config)
    if not healable(failure.error):
        return HealResult(healed=False, reason=f"not healable: {type(failure.error).__name__}")
    hint = heal_hint(failure.index, failure.description, failure.error)
    shard = Shard(
        id=f"heal-{flow.key}-{failure.index}",
        kind="exploratory",
        name=flow.name,
        goal=flow.goal,
        tags=["heal"],
    )
    loop_settings = loop_settings or AgentLoopSettings.from_config(
        config, mode="goal", max_steps=settings.heal_max_steps
    )
    work_dir = Path(work_dir)
    result = run_agent_loop(
        shard,
        _LiveDriver(driver if driver is not None else failure.driver),
        config,
        provider=_HintedProvider(provider, hint) if provider is not None else None,
        checks=(),
        settings=loop_settings,
        work_dir=work_dir,
        worker_id=worker_id,
        backend=config.backend,
        clock=clock,
    )
    session = _read_session(work_dir / "session.json")
    if session.get("goal_reached") is not True:
        reason = f"goal not reached ({session.get('stop_reason') or result.status})"
        return HealResult(healed=False, reason=reason, dropped=cache.drop(flow.key), result=result)
    try:
        new_steps = clean_steps(session.get("replay") or [])
    except IntentError as exc:
        return HealResult(healed=False, reason=f"unusable replay: {exc}", dropped=cache.drop(flow.key), result=result)
    flow.steps = [dict(step) for step in flow.steps[: failure.index]] + new_steps
    flow.heals += 1
    cache.save(flow)
    return HealResult(healed=True, reason="goal reached", flow=flow, steps=new_steps, result=result)


class Healer:
    """The `on_step_failed` hook for one cached scripted shard, and its bookkeeping.

        healer = Healer.for_shard(shard, config, provider=provider, work_dir=work_dir)
        result = run_scripted(shard, driver, config, ..., on_step_failed=healer)
        healer.finish(result)

    For a shard that is not a cached one, or with `flows.cache` or
    `flows.cache` off, the hook declines every failure and `finish` does
    nothing; with `flows.heal` off it declines too, but hits still count.
    `finish` counts a passing replay, healed or not, as a hit.
    """

    def __init__(
        self,
        shard: Shard,
        config: CampaignConfig,
        *,
        cache: FlowCache | None,
        settings: FlowsSettings,
        work_dir: Path | str,
        provider: ModelProvider | None = None,
        worker_id: str = "heal",
        loop_settings: AgentLoopSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.shard = shard
        self.config = config
        self.cache = cache
        self.settings = settings
        self.work_dir = Path(work_dir)
        self.provider = provider
        self.worker_id = worker_id
        self.loop_settings = loop_settings
        self.clock = clock
        self.attempts: list[HealResult] = []

    @classmethod
    def for_shard(
        cls,
        shard: Shard,
        config: CampaignConfig,
        *,
        work_dir: Path | str,
        provider: ModelProvider | None = None,
        cache: FlowCache | None = None,
        settings: FlowsSettings | None = None,
        worker_id: str = "heal",
        loop_settings: AgentLoopSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "Healer":
        settings = settings or FlowsSettings.from_config(config)
        if settings.cache and is_cached_shard(shard):
            cache = cache or FlowCache(cache_root(config, settings))
        else:
            cache = None
        return cls(
            shard,
            config,
            cache=cache,
            settings=settings,
            work_dir=work_dir,
            provider=provider,
            worker_id=worker_id,
            loop_settings=loop_settings,
            clock=clock,
        )

    @property
    def active(self) -> bool:
        return self.cache is not None

    @property
    def healed(self) -> bool:
        return any(attempt.healed for attempt in self.attempts)

    def __call__(self, failure: StepFailure) -> str | None:
        if self.cache is None or not self.settings.heal or not healable(failure.error):
            return None
        flow = self.cache.load(shard_key(self.shard))
        if flow is None or failure.index >= len(flow.steps):
            return None
        attempt = heal_flow(
            flow,
            failure,
            self.config,
            cache=self.cache,
            work_dir=self.work_dir / f"step-{failure.index}",
            settings=self.settings,
            provider=self.provider,
            worker_id=self.worker_id,
            loop_settings=self.loop_settings,
            clock=self.clock,
        )
        self.attempts.append(attempt)
        if not attempt.healed:
            return None
        return f"re-explored in {len(attempt.steps)} step(s); cached flow rewritten"

    def finish(self, result: WorkerResult) -> None:
        """Count a passing replay as a hit. Healing already saved or dropped the entry."""
        if self.cache is None or result.status != "passed":
            return
        try:
            self.cache.record_hit(shard_key(self.shard))
        except OSError:
            return


def _read_session(path: Path) -> dict[str, Any]:
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return session if isinstance(session, dict) else {}
