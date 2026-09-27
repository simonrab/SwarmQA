"""When a worker starts and keeps a session recording."""

from __future__ import annotations


def should_start_video(mode: str, shard_kind: str) -> bool:
    """Return whether this shard should open a recording at session start.

    `on_failure` still records from the start so the failing path is on tape.
    The caller deletes the file afterward when `keep_video` is false.
    """
    if mode == "always":
        return True
    if mode == "on_failure":
        return True
    if mode == "exploratory_only":
        return shard_kind == "exploratory"
    raise ValueError(f"unknown video mode: {mode}")


def keep_video(mode: str, shard_kind: str, shard_status: str) -> bool:
    """Return whether the finished recording should stay in the report."""
    if mode == "always":
        return True
    if mode == "on_failure":
        return shard_status in {"failed", "error"}
    if mode == "exploratory_only":
        return shard_kind == "exploratory"
    raise ValueError(f"unknown video mode: {mode}")
