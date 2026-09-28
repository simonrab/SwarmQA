"""Heuristic DecisionEvaluator — current exploratory strategy order."""

from __future__ import annotations

import re

from swarmqa.decision.protocol import Candidate, DecisionAction, Observation
from swarmqa.driver.query import walk
from swarmqa.models import ElementQuery, UIElement

# Glue words and common verbs. Remaining words are treated as control names
# when the goal does not quote a control.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "and",
        "or",
        "in",
        "on",
        "for",
        "with",
        "from",
        "into",
        "via",
        "near",
        "within",
        "without",
        "over",
        "under",
        "open",
        "click",
        "press",
        "tap",
        "find",
        "check",
        "verify",
        "confirm",
        "that",
        "this",
        "these",
        "those",
        "is",
        "be",
        "are",
        "was",
        "were",
        "button",
        "buttons",
        "menu",
        "menus",
        "control",
        "controls",
        "try",
        "using",
        "use",
        "app",
        "when",
        "then",
        "should",
        "can",
        "it",
        "its",
        "by",
        "at",
        "as",
        "if",
        "we",
        "user",
        "please",
        "just",
        "any",
        "all",
        "show",
        "see",
        "look",
        "around",
        "else",
        "broken",
        "toggle",
        "turn",
        "switch",
        "enable",
        "disable",
        "make",
        "sure",
        "does",
        "did",
        "not",
        "dont",
        "stay",
        "finish",
        "area",
        "window",
        "title",
        "updates",
        "update",
        "panel",
        "screen",
        "page",
        "view",
        "item",
        "once",
        "after",
        "before",
        "while",
        "have",
        "has",
        "had",
        "will",
        "would",
        "could",
        "about",
        "there",
        "their",
        "them",
        "they",
        "you",
        "your",
        "our",
        "out",
        "off",
        "how",
        "what",
        "which",
        "who",
        "where",
        "why",
        "also",
        "only",
        "than",
        "per",
        "etc",
        "something",
        "anything",
        "everything",
        "nothing",
        "here",
        "back",
        "again",
        "still",
        "already",
        "another",
        "other",
        "explore",
        "search",
        "navigate",
        "reach",
        "visit",
        "ensure",
        "validate",
        "given",
        "scenario",
        "label",
        "field",
        "text",
    }
)
MENU_ROLES = frozenset({"menu", "menuitem", "menubaritem", "menubar", "menu bar"})
_QUOTE = re.compile(r'"([^"]+)"|\'([^\']+)\'')

# Back-compat aliases for exploratory.py imports.
_STOPWORDS = STOPWORDS
_MENU_ROLES = MENU_ROLES


def words(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9]+", text or "")}


def shares(text: str, tokens: list[str]) -> bool:
    found = words(text)
    return any(token.lower() in found for token in tokens)


def parse_goal(text: str) -> tuple[list[str], list[str]]:
    """Return search tokens and the control names the goal expects to find.

    Quoted phrases are the expected controls. Otherwise every significant
    word is an expected control name (``Settings gear`` → Settings, gear).
    """
    quoted: list[str] = []
    for match in _QUOTE.finditer(text or ""):
        phrase = (match.group(1) or match.group(2) or "").strip()
        if phrase:
            quoted.append(phrase)
    seen: set[str] = set()
    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", (text or "").replace("'", "")):
        key = word.lower()
        if len(key) < 3 or key in STOPWORDS or key in seen:
            continue
        seen.add(key)
        tokens.append(word)
    for phrase in quoted:
        for word in re.findall(r"[A-Za-z0-9]+", phrase.replace("'", "")):
            key = word.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(word)
    expected = quoted if quoted else list(tokens)
    return tokens, expected


def element_names(element: UIElement) -> list[str]:
    names: list[str] = []
    if element.label:
        names.append(element.label)
    if element.identifier:
        names.append(element.identifier)
    return names


def matches_name(element: UIElement, name: str) -> bool:
    target = name.strip()
    if not target:
        return False
    for label in element_names(element):
        if " " in target:
            if target.lower() in label.lower() or words(target) <= words(label):
                return True
            continue
        if target.lower() in words(label):
            return True
    return False


def role_kind(role: str) -> str:
    lowered = role.lower()
    if lowered == "button":
        return "button"
    if lowered in MENU_ROLES:
        return "menu"
    return "other"


def query_for(element: UIElement) -> ElementQuery:
    return ElementQuery(
        role=element.role,
        label=element.label or None,
        identifier=element.identifier,
    )


def click_key(element: UIElement) -> str:
    return f"{element.role}|{element.label}|{element.identifier or ''}"


def click_key_from_query(query: ElementQuery) -> str:
    return f"{query.role or ''}|{query.label or ''}|{query.identifier or ''}"


def menu_paths(elements: list[UIElement], tokens: list[str]) -> list[list[str]]:
    found: list[list[str]] = []

    def walk_menus(nodes: list[UIElement], prefix: list[str]) -> None:
        for element in nodes:
            role = element.role.lower()
            label = (element.label or "").strip()
            path = prefix
            if role in MENU_ROLES and label:
                candidate = prefix + [label]
                if shares(label, tokens) or any(
                    matches_name(element, token) for token in tokens
                ):
                    found.append(candidate)
                if role in {"menu", "menuitem", "menubaritem"}:
                    path = candidate
            child_prefix = path if role in {"menubar", "menu bar", "menu"} else prefix
            if element.children:
                walk_menus(element.children, child_prefix)

    walk_menus(elements, [])
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for path in found:
        key = tuple(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def absent_expected(elements: list[UIElement], expected: list[str]) -> list[str]:
    missing: list[str] = []
    for name in expected:
        if any(matches_name(element, name) for element in walk(elements)):
            continue
        missing.append(name)
    return missing


def goal_satisfied(elements: list[UIElement], expected: list[str]) -> bool:
    if not expected:
        return True
    for name in expected:
        kinds = {
            role_kind(element.role)
            for element in walk(elements)
            if matches_name(element, name)
        }
        if not kinds or kinds == {"menu"}:
            return False
    return True


def exploration_menu_paths(
    elements: list[UIElement], tokens: list[str], expected: list[str]
) -> list[list[str]]:
    """Menu paths that share goal words, plus one-item paths for absent names."""
    paths = menu_paths(elements, tokens)
    covered = {tuple(path) for path in paths}
    for name in absent_expected(elements, expected):
        synthetic = (name,)
        if synthetic in covered:
            continue
        if any(name.lower() == part.lower() for path in paths for part in path):
            continue
        paths.append([name])
        covered.add(synthetic)
    return paths


def collect_candidates(obs: Observation) -> list[Candidate]:
    """Finite click/menu options the heuristic would consider (untried only)."""
    candidates: list[Candidate] = []
    click_i = 0
    for element in walk(obs.elements):
        if element.role.lower() != "button" or not element.enabled:
            continue
        names = element_names(element)
        if not any(shares(name, obs.tokens) for name in names):
            continue
        key = click_key(element)
        if key in obs.tried_clicks:
            continue
        label = element.label or element.identifier or element.role
        candidates.append(
            Candidate(
                kind="click",
                query=query_for(element),
                label=label,
                choice_id=f"click:{click_i}",
            )
        )
        click_i += 1

    if goal_satisfied(obs.elements, obs.expected):
        return candidates

    menu_i = 0
    for path in exploration_menu_paths(obs.elements, obs.tokens, obs.expected):
        key = tuple(path)
        if key in obs.tried_menus:
            continue
        label = " > ".join(path)
        candidates.append(
            Candidate(
                kind="menu",
                menu_path=list(path),
                label=label,
                choice_id=f"menu:{menu_i}",
            )
        )
        menu_i += 1
    return candidates


class HeuristicEvaluator:
    """Encode the pre-decision-backend exploratory strategy order.

    Default (``respect_persona=False``) — parity with classic exploratory:

    1. Prefer clicking enabled buttons that share goal words (tree order).
    2. Else menu paths that share goal words / one-item paths for absent names.
    3. Else if prototype and expected still absent → missing.
    4. Else done.

    When ``respect_persona=True`` and ``obs.persona`` is set:

    - ``expert``: try menus before clicks when both exist.
    - ``first_time``: clicks first (same as default); optionally one deliberate
      near-miss click when ≥2 matching buttons exist.
    """

    def __init__(self, *, respect_persona: bool = False):
        self.respect_persona = respect_persona
        self._wrong_picks_used = 0

    def decide(self, obs: Observation) -> DecisionAction:
        persona = (obs.persona or "").strip().lower() if self.respect_persona else ""
        expert = persona == "expert"
        first_time = persona == "first_time"

        if expert:
            menu = self._first_menu(obs)
            if menu is not None:
                return menu
            click = self._first_click(obs, first_time=False)
            if click is not None:
                return click
        else:
            click = self._first_click(obs, first_time=first_time)
            if click is not None:
                return click
            if goal_satisfied(obs.elements, obs.expected):
                return DecisionAction(kind="done", rationale="expected controls present")
            menu = self._first_menu(obs)
            if menu is not None:
                return menu

        if goal_satisfied(obs.elements, obs.expected):
            return DecisionAction(kind="done", rationale="expected controls present")

        if obs.maturity == "prototype":
            absent = absent_expected(obs.elements, obs.expected)
            if absent:
                listed = ", ".join(absent)
                return DecisionAction(
                    kind="missing",
                    label=listed,
                    rationale=f"prototype gap: {listed}",
                )

        return DecisionAction(kind="done", rationale="no remaining exploratory work")

    def _click_candidates(self, obs: Observation) -> list[tuple[UIElement, str]]:
        found: list[tuple[UIElement, str]] = []
        for element in walk(obs.elements):
            if element.role.lower() != "button" or not element.enabled:
                continue
            names = element_names(element)
            if not any(shares(name, obs.tokens) for name in names):
                continue
            key = click_key(element)
            if key in obs.tried_clicks:
                continue
            label = element.label or element.identifier or element.role
            found.append((element, label))
        return found

    def _first_click(
        self, obs: Observation, *, first_time: bool
    ) -> DecisionAction | None:
        candidates = self._click_candidates(obs)
        if not candidates:
            return None
        index = 0
        rationale_prefix = "click enabled button sharing goal words"
        if (
            first_time
            and self._wrong_picks_used < 1
            and len(candidates) >= 2
        ):
            # Deliberate near-miss: second matching button (shares a token).
            index = 1
            self._wrong_picks_used += 1
            rationale_prefix = "first_time near-miss click"
        element, label = candidates[index]
        return DecisionAction(
            kind="click",
            query=query_for(element),
            label=label,
            rationale=f"{rationale_prefix}: {label}",
        )

    def _first_menu(self, obs: Observation) -> DecisionAction | None:
        for path in exploration_menu_paths(obs.elements, obs.tokens, obs.expected):
            key = tuple(path)
            if key in obs.tried_menus:
                continue
            label = " > ".join(path)
            return DecisionAction(
                kind="menu",
                menu_path=list(path),
                label=label,
                rationale=f"try menu path: {label}",
            )
        return None


# Private aliases matching exploratory.py historical names.
_words = words
_shares = shares
_parse_goal = parse_goal
_element_names = element_names
_matches_name = matches_name
_role_kind = role_kind
_menu_paths = menu_paths
_query_for = query_for
