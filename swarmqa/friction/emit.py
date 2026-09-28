"""Build advisory ``friction_path`` findings when gates pass."""

from __future__ import annotations

from swarmqa.friction.gold import GoldResolution, GoldSource, resolve_gold
from swarmqa.friction.score import friction_score
from swarmqa.friction.session import FrictionSession
from swarmqa.models import Finding, FrictionConfig


def _severity_for(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 65:
        return "medium"
    return "low"


def _title(session: FrictionSession, gold: GoldResolution, score: int, step_ratio: float) -> str:
    if session.backtrack_count >= 2 and session.backtrack_count >= (session.steps_observed - gold.steps_gold):
        return f"{session.backtrack_count} backtracks (score {score})"
    if session.dead_end_count >= 1 and step_ratio < 1.5:
        return f"{session.dead_end_count} dead ends (score {score})"
    if session.recovery_loops >= 1 and step_ratio < 1.5:
        return f"{session.recovery_loops} recovery loops (score {score})"
    return f"{step_ratio:.1f}× gold path (score {score})"


def metrics_for(
    session: FrictionSession,
    gold: GoldResolution,
    *,
    klm: bool = True,
) -> dict[str, float | int | str | bool]:
    steps_gold = max(1, gold.steps_gold)
    step_ratio = session.steps_observed / float(steps_gold)
    # Simple KLM proxy: each observed step ≈ one operator act; ratio tracks path length.
    klm_ratio = step_ratio if klm else 1.0
    score = friction_score(
        step_ratio=step_ratio,
        backtrack_rate=session.backtrack_rate,
        recovery_loops=session.recovery_loops,
        dead_end_count=session.dead_end_count,
        klm_ratio=klm_ratio,
        rage_events=session.rage_events,
    )
    return {
        "steps_observed": session.steps_observed,
        "steps_gold": steps_gold,
        "step_ratio": step_ratio,
        "klm_ratio": klm_ratio,
        "backtrack_rate": session.backtrack_rate,
        "backtrack_count": session.backtrack_count,
        "recovery_loops": session.recovery_loops,
        "dead_end_count": session.dead_end_count,
        "rage_events": session.rage_events,
        "dead_click_events": session.dead_click_events,
        "unique_states": session.unique_states,
        "state_revisit_count": session.state_revisit_count,
        "progress_stall_max": session.progress_stall_max,
        "max_choice_entropy": session.max_choice_entropy,
        "max_actionable_count": session.max_actionable_count,
        "gold_source": gold.source,
        "persona": session.persona.name,
        "score": score,
    }


def gates_pass(
    session: FrictionSession,
    gold: GoldResolution,
    config: FrictionConfig,
    *,
    goal_directed: bool,
    score: int | None = None,
    step_ratio: float | None = None,
) -> bool:
    """Return True when an advisory friction finding should be emitted."""
    if not config.enabled:
        return False
    if not goal_directed:
        return False
    if session.steps_observed <= 0:
        return False

    metrics = metrics_for(session, gold, klm=config.klm)
    resolved_score = score if score is not None else int(metrics["score"])
    resolved_ratio = step_ratio if step_ratio is not None else float(metrics["step_ratio"])
    extra_steps = session.steps_observed - gold.steps_gold

    structural_extra = extra_steps >= config.min_extra_steps
    structural_dead_end = session.dead_end_count >= 1
    structural_recovery = session.recovery_loops >= 1
    structural_backtrack = session.backtrack_rate >= config.min_backtrack_rate
    has_structural = (
        structural_extra
        or structural_dead_end
        or structural_recovery
        or structural_backtrack
    )
    if not has_structural:
        return False

    # Happy-path FakeDriver hunts stay quiet: require min_extra_steps unless
    # the hunt already shows dead ends / recovery loops.
    if not structural_extra and not (structural_dead_end or structural_recovery):
        return False

    threshold = config.emit_threshold
    if gold.source == "synthesized":
        threshold = max(threshold, 65)
        # Synthesized gold without pathology still needs the step gate.
        if not (structural_dead_end or structural_recovery) and not structural_extra:
            return False

    if resolved_score < threshold:
        return False

    # Soft compare guard: never emit on sub-gold paths without pathology.
    if resolved_ratio <= 1.0 and not (structural_dead_end or structural_recovery):
        return False
    return True


def build_friction_finding(
    *,
    session: FrictionSession,
    gold: GoldResolution,
    config: FrictionConfig,
    worker_id: str,
    backend: str,
    shard_id: str,
    finding_id: str,
    steps: list[str],
    environment: dict[str, str],
    intent_id: str = "",
    locus: str = "",
    fingerprint_fn,
) -> Finding | None:
    """Construct a ``friction_path`` finding when gates pass, else ``None``."""
    goal_directed = bool(intent_id or locus or steps)
    metrics = metrics_for(session, gold, klm=config.klm)
    score = int(metrics["score"])
    step_ratio = float(metrics["step_ratio"])
    if not gates_pass(
        session,
        gold,
        config,
        goal_directed=goal_directed,
        score=score,
        step_ratio=step_ratio,
    ):
        return None

    title = _title(session, gold, score, step_ratio)
    target = f"{intent_id}|{locus}|{session.persona.name}"
    details = (
        f"Friction score {score}: {session.steps_observed} steps vs gold "
        f"{gold.steps_gold} ({gold.source}); backtrack_rate="
        f"{session.backtrack_rate:.2f}; recovery_loops={session.recovery_loops}; "
        f"dead_ends={session.dead_end_count}; persona={session.persona.name}. "
        "Advisory only — explorer.friction.fail_ci is reserved and not wired "
        "to fail_on yet."
    )
    return Finding(
        id=finding_id,
        title=title,
        severity=_severity_for(score),  # type: ignore[arg-type]
        kind="friction_path",
        steps=list(steps),
        fingerprint=fingerprint_fn("friction_path", title, target),
        worker_id=worker_id,
        backend=backend,
        shard_id=shard_id,
        environment=dict(environment),
        details=details,
    )


def maybe_emit_friction(
    *,
    session: FrictionSession | None,
    config: FrictionConfig,
    gold: GoldResolution | None = None,
    gold_steps: int | None = None,
    scripted_steps: int | None = None,
    shortest_success: int | None = None,
    expected_controls: int = 0,
    worker_id: str,
    backend: str,
    shard_id: str,
    finding_id: str,
    steps: list[str],
    environment: dict[str, str],
    intent_id: str = "",
    locus: str = "",
    fingerprint_fn,
) -> Finding | None:
    if session is None or not config.enabled:
        return None
    resolved = gold or resolve_gold(
        gold_steps=gold_steps,
        scripted_steps=scripted_steps,
        shortest_success=shortest_success,
        expected_controls=expected_controls,
    )
    return build_friction_finding(
        session=session,
        gold=resolved,
        config=config,
        worker_id=worker_id,
        backend=backend,
        shard_id=shard_id,
        finding_id=finding_id,
        steps=steps,
        environment=environment,
        intent_id=intent_id,
        locus=locus,
        fingerprint_fn=fingerprint_fn,
    )


# Re-export for callers that type-check gold sources beside emit.
__all__ = [
    "GoldSource",
    "build_friction_finding",
    "gates_pass",
    "maybe_emit_friction",
    "metrics_for",
]
