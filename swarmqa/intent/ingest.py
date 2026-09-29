"""C3 — turn markdown intents, JSON flows, and suite commands into shards.

See docs/CONTRACTS.md section C3 and docs/intents.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from swarmqa.errors import IntentError
from swarmqa.models import Action, CampaignConfig, ElementQuery, Shard
from swarmqa.util import parse_duration, slug

_INTENT_SUFFIXES = {".md", ".json"}
_FLOW_FIELDS = {"version", "name", "steps"}
_STEP_FIELDS = {
    "action",
    "target",
    "text",
    "keys",
    "delta",
    "path",
    "timeout_s",
    "name",
    "exists",
    "point",
    "end",
}
_QUERY_FIELDS = {"role", "label", "identifier", "value"}
_ACTIONS = {
    "click",
    "type",
    "key",
    "scroll",
    "menu",
    "wait",
    "screenshot",
    "assert",
    "launch",
    "relaunch",
    "tap_point",
    "swipe",
}

_H1 = re.compile(r"^#\s+(.+?)\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*$")
_CLICK = re.compile(r'^click(?:\s+([A-Za-z][\w-]*))?\s+"([^"]*)"\s*$')
_TYPE = re.compile(r'^type\s+"([^"]*)"\s+into(?:\s+([A-Za-z][\w-]*))?\s+"([^"]*)"\s*$')
_KEY = re.compile(r"^key\s+(\S+)\s*$")
_MENU = re.compile(r"^menu\s+(.+?)\s*$")
_WAIT = re.compile(r'^wait\s+for(?:\s+([A-Za-z][\w-]*))?\s+"([^"]*)"\s+(\S+)\s*$')
_SCROLL = re.compile(r"^scroll\s+([+-]?\d+)\s*$")
_SCREENSHOT = re.compile(r'^screenshot\s+"([^"]*)"\s*$')
_ASSERT = re.compile(
    r'^assert(?:\s+([A-Za-z][\w-]*))?\s+"([^"]*)"\s+(exists|missing|absent|not\s+exists)\s*$'
)
_LAUNCH = re.compile(r"^(launch|relaunch)\s*$")
_FRONT_MATTER_TAGS = re.compile(r"^tags\s*:\s*(.*?)\s*$", re.IGNORECASE)


def build_queue(config: CampaignConfig) -> list[Shard]:
    """Return shard units for one campaign.

    Raise IntentError when the intent set is empty or paths are missing.
    Mix markdown, JSON flows, and config.suite.command in one queue.
    """
    shards: list[Shard] = []
    for path in _expand_intents(config.intents):
        if path.suffix.lower() == ".json":
            shard = _load_json_flow(path)
            if config.coverage.scripted:
                shards.append(shard)
            continue
        shards.extend(_markdown_shards(path, config))

    if config.suite.command:
        shards.append(
            Shard(
                id="",
                kind="suite",
                name="suite",
                suite_command=config.suite.command,
            )
        )
    if config.visual.enabled or config.coverage.visual:
        shards.append(_visual_shard(config))

    shards = _order_shards(shards, config.shard_strategy)
    if not shards:
        raise IntentError("intent set is empty")
    _assign_ids(shards)
    return shards


def parse_step_line(line: str, *, origin: str) -> Action:
    """Parse one markdown step, with or without a leading bullet hyphen."""
    text = _strip_bullet(line.strip())
    if not text:
        raise IntentError(f"unrecognized step in {origin}: {line.strip()}")

    match = _CLICK.fullmatch(text)
    if match:
        role, label = match.group(1), match.group(2)
        return Action(action="click", target=_query(role, label))

    match = _TYPE.fullmatch(text)
    if match:
        typed, role, label = match.group(1), match.group(2), match.group(3)
        return Action(action="type", text=typed, target=_query(role, label))

    match = _KEY.fullmatch(text)
    if match:
        keys = [part for part in match.group(1).split("+") if part]
        if not keys:
            raise IntentError(f"unrecognized step in {origin}: {text}")
        return Action(action="key", keys=keys)

    match = _MENU.fullmatch(text)
    if match:
        parts = [part.strip() for part in match.group(1).split(">") if part.strip()]
        if not parts:
            raise IntentError(f"unrecognized step in {origin}: {text}")
        return Action(action="menu", path=parts)

    match = _WAIT.fullmatch(text)
    if match:
        role, label, duration = match.group(1), match.group(2), match.group(3)
        try:
            timeout_s = parse_duration(duration)
        except ValueError as exc:
            raise IntentError(f"invalid wait duration in {origin}: {text}") from exc
        return Action(action="wait", target=_query(role, label), timeout_s=timeout_s)

    match = _SCROLL.fullmatch(text)
    if match:
        return Action(action="scroll", delta=int(match.group(1)))

    match = _SCREENSHOT.fullmatch(text)
    if match:
        return Action(action="screenshot", name=match.group(1))

    match = _ASSERT.fullmatch(text)
    if match:
        role, label, flag = match.group(1), match.group(2), match.group(3)
        exists = flag == "exists"
        return Action(action="assert", target=_query(role, label), exists=exists)

    match = _LAUNCH.fullmatch(text)
    if match:
        if match.group(1) == "launch":
            return Action(action="launch")
        return Action(action="relaunch")

    raise IntentError(f"unrecognized step in {origin}: {text}")


def action_from_dict(data: object, *, origin: str) -> Action:
    """Build an Action from a version-1 flow step object."""
    if not isinstance(data, dict):
        raise IntentError(f"flow step must be an object: {origin}")
    extra = set(data) - _STEP_FIELDS
    if extra:
        name = sorted(extra)[0]
        raise IntentError(f"unknown step field {name}: {origin}")
    action = data.get("action")
    if action not in _ACTIONS:
        raise IntentError(f"unknown action {action!r}: {origin}")
    point = _optional_point(data.get("point"), "point", origin)
    end = _optional_point(data.get("end"), "end", origin)
    if action in ("tap_point", "swipe") and point is None:
        raise IntentError(f"{action} needs point: {origin}")
    if action == "swipe" and end is None:
        raise IntentError(f"swipe needs end: {origin}")
    return Action(
        action=action,
        point=point,
        end=end,
        target=_target_from_dict(data.get("target"), origin=origin),
        text=_optional_str(data.get("text"), "text", origin),
        keys=_string_list(data.get("keys"), "keys", origin),
        delta=_optional_int(data.get("delta"), "delta", origin),
        path=_string_list(data.get("path"), "path", origin),
        timeout_s=_optional_number(data.get("timeout_s"), "timeout_s", origin),
        name=_optional_str(data.get("name"), "name", origin),
        exists=_optional_bool(data.get("exists"), "exists", origin),
    )


def action_to_dict(action: Action) -> dict:
    """Serialize an action with schema fields only, omitting empty values."""
    payload: dict = {"action": action.action}
    if action.target is not None:
        target: dict[str, str] = {}
        if action.target.role is not None:
            target["role"] = action.target.role
        if action.target.label is not None:
            target["label"] = action.target.label
        if action.target.identifier is not None:
            target["identifier"] = action.target.identifier
        if action.target.value is not None:
            target["value"] = action.target.value
        if target:
            payload["target"] = target
    if action.text is not None:
        payload["text"] = action.text
    if action.keys:
        payload["keys"] = list(action.keys)
    if action.delta is not None:
        payload["delta"] = action.delta
    if action.path:
        payload["path"] = list(action.path)
    if action.timeout_s is not None:
        payload["timeout_s"] = action.timeout_s
    if action.name is not None:
        payload["name"] = action.name
    if action.exists is not None:
        payload["exists"] = action.exists
    if action.point is not None:
        payload["point"] = [float(action.point[0]), float(action.point[1])]
    if action.end is not None:
        payload["end"] = [float(action.end[0]), float(action.end[1])]
    return payload


def _expand_intents(intents: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in intents:
        if raw is None or not str(raw).strip():
            raise IntentError("intent path is empty")
        path = Path(str(raw))
        if not path.exists():
            raise IntentError(f"intent not found: {raw}")
        if path.is_dir():
            found = [
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in _INTENT_SUFFIXES
            ]
            found.sort(key=lambda item: item.relative_to(path).as_posix())
            files.extend(found)
        elif path.is_file():
            if path.suffix.lower() not in _INTENT_SUFFIXES:
                raise IntentError(f"unsupported intent file: {path}")
            files.append(path)
        else:
            raise IntentError(f"unsupported intent path: {path}")
    return files


def _markdown_shards(path: Path, config: CampaignConfig) -> list[Shard]:
    parsed = _parse_markdown(path)
    tags = _intent_tags(parsed["name"], parsed["tags"])
    shards: list[Shard] = []
    if parsed["actions"] is not None:
        if config.coverage.scripted:
            shards.append(
                Shard(
                    id="",
                    kind="scripted",
                    name=parsed["name"],
                    source_path=str(path),
                    actions=list(parsed["actions"]),
                    goal=parsed["goal"],
                    constraints=list(parsed["constraints"]),
                    tags=list(tags),
                )
            )
        # A scripted markdown intent also contributes an exploratory seed when
        # that coverage flag is on. JSON flows do not. The seed carries the
        # scripted step count as its friction gold path.
        if config.coverage.exploratory:
            seed_tags = list(tags)
            if not any(tag.startswith(("gold_steps:", "gold:")) for tag in seed_tags):
                seed_tags.append(f"gold_steps:{len(parsed['actions'])}")
            shards.append(
                Shard(
                    id="",
                    kind="exploratory",
                    name=parsed["name"],
                    source_path=str(path),
                    goal=parsed["goal"],
                    constraints=list(parsed["constraints"]),
                    seed="explore",
                    tags=seed_tags,
                )
            )
        return shards
    if config.coverage.exploratory:
        shards.append(
            Shard(
                id="",
                kind="exploratory",
                name=parsed["name"],
                source_path=str(path),
                goal=parsed["goal"],
                constraints=list(parsed["constraints"]),
                tags=list(tags),
            )
        )
    return shards


def _intent_tags(name: str, declared: list[str]) -> list[str]:
    """Shard tags: declared tags (front matter, then `## Tags`), then `flow:<slug>`.

    Tags are trimmed, lower-cased, inner whitespace becomes `-`, and
    duplicates are dropped in order.
    """
    out: list[str] = []
    for raw in [*declared, f"flow:{slug(name)}"]:
        tag = str(raw).strip().strip("'\"").strip().lower()
        tag = re.sub(r"\s*:\s*", ":", tag)
        tag = re.sub(r"\s+", "-", tag)
        if tag and tag not in out:
            out.append(tag)
    return out


def _split_tags(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [part.strip() for part in text.split(",") if part.strip()]


def _front_matter(lines: list[str]) -> tuple[list[str], int]:
    """Tags from a leading `---` block, and the number of lines it spans (0 when absent).

    Only `tags:` is read: `tags: [a, b]`, `tags: a, b`, or `tags:` followed
    by `- a` lines. Other keys are ignored and kept out of the goal.
    """
    if not lines or lines[0].strip() != "---":
        return [], 0
    for end in range(1, len(lines)):
        if lines[end].strip() in ("---", "..."):
            break
    else:
        return [], 0
    tags: list[str] = []
    in_list = False
    for line in lines[1:end]:
        match = _FRONT_MATTER_TAGS.match(line.strip())
        if match:
            tags.extend(_split_tags(match.group(1)))
            in_list = not match.group(1)
            continue
        stripped = line.strip()
        if in_list and stripped.startswith("- "):
            tags.append(stripped[2:].strip())
            continue
        in_list = False
    return tags, end + 1


def _parse_markdown(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise IntentError(f"cannot read intent: {path}") from exc

    title = ""
    body_lines: list[str] = []
    constraints: list[str] = []
    actions: list[Action] = []
    section = "body"
    saw_steps = False

    lines = text.splitlines()
    tags, skip = _front_matter(lines)
    for line_no, line in enumerate(lines, start=1):
        if line_no <= skip:
            continue
        h2 = _H2.match(line)
        if h2:
            heading = h2.group(1).strip().rstrip(":").strip().lower()
            if heading == "steps":
                section = "steps"
                saw_steps = True
            elif heading == "constraints":
                section = "constraints"
            elif heading == "tags":
                section = "tags"
            else:
                section = "body"
                body_lines.append(h2.group(1).strip())
            continue
        h1 = _H1.match(line)
        if h1:
            if not title:
                title = h1.group(1).strip()
            elif section == "body":
                body_lines.append(h1.group(1).strip())
            continue
        if section == "constraints":
            item = _constraint_text(line)
            if item:
                constraints.append(item)
            continue
        if section == "tags":
            item = _constraint_text(line)
            if item:
                tags.extend(_split_tags(item))
            continue
        if section == "steps":
            if not line.strip():
                continue
            origin = f"{path}:{line_no}"
            actions.append(parse_step_line(line, origin=origin))
            continue
        body_lines.append(line)

    body = re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines).strip())
    name = title or path.stem
    goal_parts = [part for part in (title, body) if part]
    goal = "\n\n".join(goal_parts) if goal_parts else name
    return {
        "name": name,
        "goal": goal,
        "constraints": constraints,
        "actions": actions if saw_steps and actions else None,
        "tags": tags,
    }


def _load_json_flow(path: Path) -> Shard:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise IntentError(f"cannot read flow: {path}") from exc
    except json.JSONDecodeError as exc:
        raise IntentError(f"invalid flow JSON: {path}") from exc
    if not isinstance(data, dict):
        raise IntentError(f"flow must be a JSON object: {path}")
    version = data.get("version")
    if type(version) is not int or version != 1:
        raise IntentError(f"flow version must be 1: {path}")
    extra = set(data) - _FLOW_FIELDS
    if extra:
        name = sorted(extra)[0]
        raise IntentError(f"unknown flow field {name}: {path}")
    flow_name = data.get("name")
    if not isinstance(flow_name, str) or not flow_name.strip():
        raise IntentError(f"flow name must be a non-empty string: {path}")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise IntentError(f"flow steps must be a list: {path}")
    actions = [action_from_dict(step, origin=str(path)) for step in steps]
    return Shard(
        id="",
        kind="scripted",
        name=flow_name.strip(),
        source_path=str(path),
        actions=actions,
        tags=_intent_tags(flow_name.strip(), []),
    )


def _visual_shard(config: CampaignConfig) -> Shard:
    baseline = Path(config.visual.baseline_dir)
    names: list[str] = []
    if baseline.is_dir():
        names = sorted(
            item.stem
            for item in baseline.iterdir()
            if item.is_file() and item.suffix.lower() == ".png"
        )
    return Shard(
        id="",
        kind="visual",
        name="visual",
        source_path=str(baseline) if baseline.is_dir() else None,
        visual_names=names,
    )


def _order_shards(shards: list[Shard], strategy: str) -> list[Shard]:
    if strategy == "intent":
        return list(shards)
    if strategy == "suite":
        return [shard for shard in shards if shard.kind == "suite"] + [
            shard for shard in shards if shard.kind != "suite"
        ]
    if strategy == "exploratory_seed":
        return [shard for shard in shards if shard.kind == "exploratory"] + [
            shard for shard in shards if shard.kind != "exploratory"
        ]
    raise IntentError(f"unknown shard_strategy: {strategy}")


def _assign_ids(shards: list[Shard]) -> None:
    for index, shard in enumerate(shards, start=1):
        shard.id = f"s-{index}-{slug(shard.name)}"


def _strip_bullet(text: str) -> str:
    if text.startswith("- "):
        return text[2:].strip()
    if text.startswith("* "):
        return text[2:].strip()
    return text


def _constraint_text(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    return _strip_bullet(stripped) or None


def _query(role: str | None, label: str) -> ElementQuery:
    return ElementQuery(role=role, label=label)


def _target_from_dict(value: object, *, origin: str) -> ElementQuery | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise IntentError(f"step target must be an object: {origin}")
    extra = set(value) - _QUERY_FIELDS
    if extra:
        name = sorted(extra)[0]
        raise IntentError(f"unknown target field {name}: {origin}")
    return ElementQuery(
        role=_optional_str(value.get("role"), "role", origin),
        label=_optional_str(value.get("label"), "label", origin),
        identifier=_optional_str(value.get("identifier"), "identifier", origin),
        value=_optional_str(value.get("value"), "value", origin),
    )


def _optional_str(value: object, field: str, origin: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IntentError(f"step {field} must be a string: {origin}")
    return value


def _optional_bool(value: object, field: str, origin: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise IntentError(f"step {field} must be a boolean: {origin}")
    return value


def _optional_int(value: object, field: str, origin: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise IntentError(f"step {field} must be an integer: {origin}")
    return value


def _optional_point(value: object, field: str, origin: str) -> tuple[float, float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) not in {int, float} for item in value)
    ):
        raise IntentError(f"{field} must be [x, y]: {origin}")
    return (float(value[0]), float(value[1]))


def _optional_number(value: object, field: str, origin: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise IntentError(f"step {field} must be a number: {origin}")
    return float(value)


def _string_list(value: object, field: str, origin: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IntentError(f"step {field} must be a list of strings: {origin}")
    return list(value)
