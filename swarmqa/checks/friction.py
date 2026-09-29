"""Friction as a check: per-step metering plus a session-end verdict.

Friction is a property of a whole session (path length against a gold
path), so it cannot be decided from one step. `FrictionCheck.run` feeds
every step into a `FrictionSession` and records the step's KLM operator
cost, returning no issues. The agent loop calls `finish()` once when the
session ends; it returns at most one advisory `friction_path` issue through
the same gates as `swarmqa.friction.emit`.

The KLM term measures operator cost per step (a typing step costs more than
a tap) relative to the gold path, so it no longer repeats the step ratio.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarmqa.checks.protocol import CheckIssue, StepContext
from swarmqa.friction.emit import gates_pass, metrics_for, resolve_allow_ratio, suggests_wizard
from swarmqa.friction.gold import GoldResolution, resolve_gold, scripted_steps_from_shard
from swarmqa.friction.session import FrictionSession
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import Action, FrictionConfig, Shard

# Keystroke-Level Model operator times in seconds (Card, Moran & Newell):
# M mental preparation, P point, B button press and release, K one keystroke
# (average non-secretarial typist), D a short drag.
KLM_M = 1.35
KLM_P = 1.1
KLM_B = 0.2
KLM_K = 0.28
KLM_D = 0.3
TAP_SECONDS = KLM_M + KLM_P + KLM_B


def klm_seconds(kind: str, *, text: str | None = None, keys: list[str] | None = None) -> float | None:
    """KLM time for one action kind (agent `StepDecision` or recorded `Action`); None when it is not a user act."""
    if kind in ("tap", "tap_point", "click", "back", "menu"):
        return TAP_SECONDS
    if kind == "type":
        return TAP_SECONDS + KLM_K * len(text or "")
    if kind in ("swipe", "scroll"):
        return TAP_SECONDS + KLM_D
    if kind == "key":
        return KLM_M + KLM_K * max(1, len(keys or []))
    return None


def klm_ratio_for(observed: list[float], gold: list[float] | None = None) -> float | None:
    """Mean observed operator time per step over the gold mean (a tap when gold is unknown).

    None when nothing was observed. Path length is left to `step_ratio`.
    """
    if not observed:
        return None
    gold_mean = (sum(gold) / len(gold)) if gold else TAP_SECONDS
    if gold_mean <= 0:
        return None
    return (sum(observed) / len(observed)) / gold_mean


def gold_klm_from_actions(actions: list[Action]) -> list[float]:
    out: list[float] = []
    for action in actions:
        seconds = klm_seconds(action.action, text=action.text, keys=action.keys)
        if seconds is not None:
            out.append(seconds)
    return out


@dataclass
class FrictionContext:
    """What the session was for: used for gold, allow-ratio, and wizard rules."""

    intent_id: str = ""
    locus: str = ""
    tags: tuple[str, ...] = ()


class FrictionCheck:
    name = "friction"
    per_screen = False

    def __init__(
        self,
        config: FrictionConfig | None = None,
        *,
        persona: str | None = None,
        gold: GoldResolution | None = None,
        gold_actions: list[Action] | None = None,
        context: FrictionContext | None = None,
    ):
        self.config = config or FrictionConfig()
        personas = self.config.personas or ["expert"]
        self.session = FrictionSession(persona=persona or personas[0])
        self.context = context or FrictionContext()
        self.gold = gold
        self.gold_klm = gold_klm_from_actions(gold_actions or [])
        self.observed_klm: list[float] = []
        self.last_error: str | None = None

    @classmethod
    def for_shard(cls, shard: Shard, config: FrictionConfig | None = None, **kwargs) -> "FrictionCheck":
        """Build from a shard: gold from its actions or `gold_steps:` tag, context from its id, goal and tags."""
        gold = resolve_gold(scripted_steps=scripted_steps_from_shard(shard))
        context = FrictionContext(
            intent_id=shard.id or shard.name or "",
            locus=shard.goal,
            tags=tuple(shard.tags or ()),
        )
        return cls(config, gold=gold, gold_actions=list(shard.actions), context=context, **kwargs)

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        action = ctx.action
        if action is not None and action.kind in ("done", "give_up"):
            # Not a user act: the loop is only reporting its verdict.
            self.session.goal_reached = self.session.goal_reached or action.kind == "done"
            return []
        if ctx.crashed and not ctx.after.tree:
            return []  # the app died: there is no screen to meter
        kind, target_key = _action_key(action)
        success = not (ctx.error or ctx.crashed)
        self.session.observe(ctx.after.tree, kind, target_key, success)
        if action is not None:
            seconds = klm_seconds(action.kind, text=action.text, keys=action.keys)
            if seconds is not None:
                self.observed_klm.append(seconds)
        return []

    def klm_ratio(self) -> float | None:
        return klm_ratio_for(self.observed_klm, self.gold_klm)

    def metrics(self) -> dict[str, float | int | str | bool]:
        context = self.context
        wizard = suggests_wizard(intent_id=context.intent_id, locus=context.locus, tags=list(context.tags))
        return metrics_for(
            self.session,
            self._gold(),
            klm=self.config.klm,
            wizard_discount=wizard,
            klm_ratio=self.klm_ratio(),
        )

    def finish(self) -> list[CheckIssue]:
        """Session-end verdict: one advisory `friction_path` issue when the gates pass."""
        config = self.config
        context = self.context
        session = self.session
        if not config.enabled or session.steps_observed <= 0:
            return []
        gold = self._gold()
        tags = list(context.tags)
        metrics = self.metrics()
        score = int(metrics["score"])
        ratio = float(metrics["step_ratio"])
        allow = resolve_allow_ratio(config, intent_id=context.intent_id, tags=tags)
        goal_directed = bool(context.intent_id or context.locus)
        if not gates_pass(
            session,
            gold,
            config,
            goal_directed=goal_directed,
            score=score,
            step_ratio=ratio,
            allow_ratio=allow,
            intent_id=context.intent_id,
            locus=context.locus,
            tags=tags,
            klm_ratio=self.klm_ratio(),
        ):
            return []
        severity = "high" if score >= 80 else "medium" if score >= 65 else "low"
        where = context.intent_id or " ".join(context.locus.split())[:60] or "session"
        details = (
            f"Friction score {score}: {session.steps_observed} steps vs gold {gold.steps_gold} "
            f"({gold.source}), {ratio:.1f}x; klm_ratio={float(metrics['klm_ratio']):.2f}; "
            f"backtrack_rate={session.backtrack_rate:.2f}; recovery_loops={session.recovery_loops}; "
            f"dead_ends={session.dead_end_count}; persona={session.persona.name}."
        )
        # The title stays free of numbers so score drift does not fork the finding.
        return [
            CheckIssue(
                kind="friction_path",
                category="confusing",
                title=f"High-friction path ({session.persona.name}): {where}",
                severity=severity,  # type: ignore[arg-type]
                confidence=0.7,
                advisory=True,
                details=details,
                check=self.name,
                extra={key: str(value) for key, value in metrics.items()},
            )
        ]

    def _gold(self) -> GoldResolution:
        if self.gold is not None:
            return self.gold
        return resolve_gold()


def _action_key(action: StepDecision | None) -> tuple[str, str | None]:
    """Map an agent action onto FrictionSession's vocabulary (click / menu / other)."""
    if action is None:
        return "read", None
    kind = "click" if action.kind in ("tap", "tap_point") else action.kind
    target = action.target
    if target is not None:
        return kind, target.identifier or target.label or target.role
    if action.point is not None:
        return kind, f"{round(action.point[0])},{round(action.point[1])}"
    return kind, None
