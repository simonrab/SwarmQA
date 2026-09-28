"""C8 — live campaign status file.

See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

import types
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from swarmqa.errors import AQAError
from swarmqa.models import CampaignStatus
from swarmqa.serialize import dump_json, load_json


def write_status(status: CampaignStatus, report_dir: Path) -> Path:
    """Write report_dir/status.json."""
    path = Path(report_dir) / "status.json"
    dump_json(status, path)
    return path


def read_status(report_root: Path, campaign_id: str | None = None) -> str:
    """Return a human-readable status block for `aqa status`."""
    root = resolve_campaign_dir(Path(report_root), campaign_id)
    status = load_status(root)
    if status is None:
        raise AQAError(f"no status.json under {root}")
    return format_status_block(status)


def load_status(report_dir: Path) -> CampaignStatus | None:
    path = Path(report_dir) / "status.json"
    if not path.is_file():
        return None
    return model_from_plain(CampaignStatus, load_json(path))


def resolve_campaign_dir(report_root: Path, campaign_id: str | None = None) -> Path:
    root = Path(report_root)
    if campaign_id:
        return root / Path(campaign_id).name
    if not root.is_dir():
        raise AQAError(f"report root not found: {root}")
    campaigns = sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))
    if not campaigns:
        raise AQAError(f"no campaigns in {root}")
    return campaigns[-1]


def format_amount(value: float | None) -> str:
    if value is None:
        return "none"
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_campaign_line(status: CampaignStatus) -> str:
    active = len(status.active_workers)
    spent = format_amount(status.spend_estimated)
    cap = format_amount(status.spend_cap)
    return (
        f"[campaign {status.campaign_id}] queue={status.queue_depth} "
        f"active={active} backend={status.backend} "
        f"spend={spent}/{cap} {status.spend_currency}"
    )


def format_worker_line(worker_id: str, shard_name: str, mode: str, step: str) -> str:
    return f"[worker {worker_id}] shard={shard_name} mode={mode} step={step}"


def format_status_block(status: CampaignStatus) -> str:
    lines = [format_campaign_line(status)]
    for worker in status.active_workers:
        lines.append(
            format_worker_line(worker.worker_id, worker.shard_name, worker.mode, worker.step)
        )
    return "\n".join(lines) + "\n"


def model_from_plain(cls: type, data: Any) -> Any:
    """Rebuild a dataclass tree from JSON written by `dump_json`."""
    if data is None:
        return None
    if not is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for name, hint in hints.items():
        if name not in data:
            continue
        kwargs[name] = _convert(hint, data[name])
    return cls(**kwargs)


def _convert(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return _convert(args[0], value)
        for arg in args:
            if isinstance(arg, type) and is_dataclass(arg) and isinstance(value, dict):
                return _convert(arg, value)
        return value
    if origin is list:
        inner = get_args(hint)[0] if get_args(hint) else Any
        return [_convert(inner, item) for item in value]
    if origin is dict:
        args = get_args(hint)
        if len(args) == 2:
            key_type, value_type = args
            return {_convert(key_type, key): _convert(value_type, item) for key, item in value.items()}
        return dict(value)
    if origin is tuple:
        return tuple(value)
    if isinstance(hint, type) and is_dataclass(hint):
        return model_from_plain(hint, value)
    return value
