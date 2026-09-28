"""Per-hunt friction counters from accessibility-tree fingerprints."""

from __future__ import annotations

import hashlib
import math
from collections import Counter

from swarmqa.driver.query import walk
from swarmqa.friction.personas import PersonaPolicy, persona_named
from swarmqa.models import UIElement

_WINDOW_ROLES = frozenset({"window", "dialog", "sheet", "drawer"})
_ACTIONABLE_ROLES = frozenset(
    {
        "button",
        "menuitem",
        "menu",
        "checkbox",
        "radio",
        "link",
        "tab",
        "combobox",
        "popupbutton",
        "slider",
        "switch",
        "textfield",
        "searchfield",
    }
)


def state_fingerprint(tree: list[UIElement]) -> str:
    """Hash window-ish labels plus sorted actionable role/label/enabled."""
    windows: list[str] = []
    actionables: list[tuple[str, str, str]] = []
    for element in walk(tree):
        role = (element.role or "").strip().lower()
        label = (element.label or "").strip()
        if role in _WINDOW_ROLES:
            windows.append(label.lower())
        if role in _ACTIONABLE_ROLES:
            actionables.append((role, label.lower(), "1" if element.enabled else "0"))
    windows.sort()
    actionables.sort()
    payload = "windows:" + "|".join(windows) + "\nactions:" + "|".join(
        f"{role}:{label}:{enabled}" for role, label, enabled in actionables
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _actionable_elements(tree: list[UIElement]) -> list[UIElement]:
    return [
        element
        for element in walk(tree)
        if (element.role or "").strip().lower() in _ACTIONABLE_ROLES
    ]


def _choice_entropy(labels: list[str]) -> float:
    if not labels:
        return 0.0
    counts = Counter(labels)
    total = float(len(labels))
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


class FrictionSession:
    """Accumulate real-user-like navigation pathology for one exploratory hunt."""

    def __init__(self, *, persona: str | PersonaPolicy = "expert"):
        if isinstance(persona, PersonaPolicy):
            self.persona = persona
        else:
            self.persona = persona_named(persona)
        self.steps_observed = 0
        self.unique_state_hashes: set[str] = set()
        self.state_revisit_count = 0
        self.backtrack_count = 0
        self.recovery_loops = 0
        self.dead_end_count = 0
        self.rage_events = 0
        self.dead_click_events = 0
        self.progress_stall_max = 0
        self.max_choice_entropy = 0.0
        self.max_actionable_count = 0
        self._history: list[str] = []
        self._stall_run = 0
        self._same_target_stalls: dict[str, int] = {}
        self._pending_recovery_from: str | None = None
        self.goal_reached = False

    @property
    def unique_states(self) -> int:
        return len(self.unique_state_hashes)

    @property
    def backtrack_rate(self) -> float:
        if self.steps_observed <= 0:
            return 0.0
        return self.backtrack_count / float(self.steps_observed)

    def observe(
        self,
        tree: list[UIElement],
        action_kind: str,
        target_key: str | None,
        success: bool,
    ) -> str:
        """Update counters from one tree snapshot after a tree read / click / menu."""
        fingerprint = state_fingerprint(tree)
        actionables = _actionable_elements(tree)
        enabled = [element for element in actionables if element.enabled]
        labels = [(element.label or "").strip().lower() for element in enabled]
        entropy = _choice_entropy([label for label in labels if label])
        self.max_choice_entropy = max(self.max_choice_entropy, entropy)
        self.max_actionable_count = max(self.max_actionable_count, len(enabled))

        self.steps_observed += 1
        previous = self._history[-1] if self._history else None

        if previous is None:
            self.unique_state_hashes.add(fingerprint)
            self._stall_run = 0
        elif fingerprint == previous:
            self._stall_run += 1
            self.progress_stall_max = max(self.progress_stall_max, self._stall_run)
            if target_key:
                stalls = self._same_target_stalls.get(target_key, 0) + 1
                self._same_target_stalls[target_key] = stalls
                if stalls >= 3:
                    self.rage_events += 1
                    self._same_target_stalls[target_key] = 0
        else:
            self._stall_run = 0
            self._same_target_stalls.clear()
            if fingerprint in self.unique_state_hashes:
                self.state_revisit_count += 1
                self.backtrack_count += 1
                if self._pending_recovery_from is not None:
                    self.recovery_loops += 1
                    self._pending_recovery_from = None
                else:
                    self._pending_recovery_from = previous
            else:
                self.unique_state_hashes.add(fingerprint)
                if self._pending_recovery_from is not None:
                    # Left the recovered state toward somewhere new.
                    self._pending_recovery_from = None

        if not success:
            if action_kind in ("click", "menu"):
                self.dead_end_count += 1
            if action_kind == "click":
                self.dead_click_events += 1
        elif action_kind in ("click", "menu") and not enabled:
            self.dead_end_count += 1

        self._history.append(fingerprint)
        return fingerprint
