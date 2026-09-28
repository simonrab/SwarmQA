"""System One — HTTP typed chooser over finite a11y candidates."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from swarmqa.decision.heuristic import collect_candidates
from swarmqa.decision.protocol import Candidate, DecisionAction, Observation
from swarmqa.decision.serialize_tree import summarize_tree
from swarmqa.models import DecisionConfig, SystemOneConfig


class SystemOneError(Exception):
    """Raised when System One cannot produce a usable choice."""


def candidate_to_action(candidate: Candidate, *, rationale: str) -> DecisionAction:
    return DecisionAction(
        kind=candidate.kind,  # type: ignore[arg-type]
        query=candidate.query,
        menu_path=list(candidate.menu_path) if candidate.menu_path else None,
        label=candidate.label,
        rationale=rationale,
    )


def candidates_payload(candidates: list[Candidate]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row: dict[str, Any] = {
            "id": candidate.choice_id,
            "kind": candidate.kind,
            "label": candidate.label,
        }
        if candidate.query is not None:
            row["query"] = {
                "role": candidate.query.role,
                "label": candidate.query.label,
                "identifier": candidate.query.identifier,
            }
        if candidate.menu_path is not None:
            row["menu_path"] = list(candidate.menu_path)
        rows.append(row)
    return rows


def resolve_choice(
    candidates: list[Candidate],
    *,
    choice_id: str | None,
    confidence: float,
    min_confidence: float,
) -> Candidate:
    if choice_id is None or choice_id == "":
        raise SystemOneError("missing choice_id")
    if confidence < min_confidence:
        raise SystemOneError(
            f"confidence {confidence} below min_confidence {min_confidence}"
        )
    for candidate in candidates:
        if candidate.choice_id == choice_id:
            return candidate
    raise SystemOneError(f"unknown choice_id: {choice_id}")


class SystemOneEvaluator:
    """POST goal + candidates to an HTTP endpoint; map ``choice_id`` back.

    Endpoint URL comes from the env var named by ``system_one.endpoint_env``
    (default ``AQA_SYSTEM_ONE_ENDPOINT``). Optional API key from
    ``system_one.api_key_env`` (default ``AQA_SYSTEM_ONE_API_KEY``).

    Stdlib only — no vendor SDKs. On any error or low confidence, raises
    ``SystemOneError`` so the cascade can fail open to the heuristic.
    """

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        system_one: SystemOneConfig | None = None,
        timeout_s: float | None = None,
    ):
        self.config = config or DecisionConfig()
        self.system_one = system_one or self.config.system_one
        self.timeout_s = (
            self.config.model_timeout_s if timeout_s is None else timeout_s
        )

    def decide(self, obs: Observation) -> DecisionAction:
        candidates = collect_candidates(obs)
        if not candidates:
            raise SystemOneError("no candidates")

        endpoint = (os.environ.get(self.system_one.endpoint_env) or "").strip()
        if not endpoint:
            raise SystemOneError(
                f"endpoint env {self.system_one.endpoint_env} is unset"
            )

        body = {
            "goal": obs.goal,
            "candidates": candidates_payload(candidates),
            "tree": summarize_tree(
                obs.elements, max_depth=self.system_one.include_tree_depth
            ),
            "min_confidence": self.system_one.min_confidence,
        }
        api_key = (os.environ.get(self.system_one.api_key_env) or "").strip() or None
        payload = self._post(endpoint, body, api_key=api_key)
        choice_id = payload.get("choice_id") or payload.get("id")
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise SystemOneError("invalid confidence") from exc

        chosen = resolve_choice(
            candidates,
            choice_id=choice_id if isinstance(choice_id, str) else None,
            confidence=confidence,
            min_confidence=self.system_one.min_confidence,
        )
        return candidate_to_action(
            chosen,
            rationale=f"system_one choice_id={chosen.choice_id} confidence={confidence}",
        )

    def _post(
        self, url: str, body: dict[str, Any], *, api_key: str | None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise SystemOneError(f"HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 — fail-open boundary
            raise SystemOneError(str(exc) or exc.__class__.__name__) from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise SystemOneError("invalid JSON response") from exc
        if not isinstance(parsed, dict):
            raise SystemOneError("response must be an object")
        return parsed
