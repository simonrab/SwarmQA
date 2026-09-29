"""Path helpers shared by the evidence writers.

Finding media paths are relative to the campaign report directory when the
file lives inside it, and absolute otherwise.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Callable

REPLAY_COMMAND = "aqa replay"

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def resolve(campaign_dir: Path, path: str | Path) -> Path:
    """Absolute path for a finding path that may be campaign-relative."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else Path(campaign_dir) / candidate


def public(campaign_dir: Path, path: str | Path) -> str:
    """Campaign-relative POSIX path when `path` is inside the campaign, else absolute."""
    absolute = resolve(campaign_dir, path)
    try:
        return absolute.resolve().relative_to(Path(campaign_dir).resolve()).as_posix()
    except ValueError:
        return str(absolute)


def default_runner(args: list[str]) -> "subprocess.CompletedProcess[str]":
    """Run a binary with captured text output and no shell."""
    return subprocess.run(args, capture_output=True, text=True, check=False)


def repro_command(replay_path: str | Path) -> str:
    """`aqa replay <path>`, relative to the working directory when possible."""
    path = Path(replay_path)
    try:
        shown = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        shown = str(path)
    return f"{REPLAY_COMMAND} {shlex.quote(shown)}"
