"""C3 — write the owned JSON recorded-flow document.

See docs/CONTRACTS.md section C3 and docs/intents.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from swarmqa.errors import IntentError
from swarmqa.intent.ingest import action_from_dict, action_to_dict, parse_step_line
from swarmqa.models import Action


def record_flow(
    *,
    app_path: str,
    out_path: Path,
    name: str = "recorded-flow",
    interactive: bool = False,
    events: list[dict] | None = None,
) -> Path:
    """Write a version-1 JSON flow to `out_path` and return that path.

    `events` writes those action dicts. Interactive mode reads line-oriented
    commands from stdin until `done` or EOF. Otherwise the file contains a
    single launch step. `app_path` selects the app being recorded; the flow
    document itself stays the version-1 action list.
    """
    del app_path  # recorded flows are app-agnostic action lists
    flow_name = name.strip()
    if not flow_name:
        raise IntentError("flow name must be a non-empty string")

    if events is not None:
        if not isinstance(events, list):
            raise IntentError("events must be a list of action objects")
        steps = [
            action_to_dict(action_from_dict(event, origin=str(out_path)))
            for event in events
        ]
    elif interactive:
        steps = [_read_interactive_step(line) for line in _interactive_lines()]
    else:
        steps = [action_to_dict(Action(action="launch"))]

    document = {"version": 1, "name": flow_name, "steps": steps}
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _interactive_lines() -> list[str]:
    lines: list[str] = []
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        text = line.strip()
        if not text:
            continue
        if text == "done":
            break
        lines.append(text)
    return lines


def _read_interactive_step(line: str) -> dict:
    return action_to_dict(parse_step_line(line, origin="stdin"))
