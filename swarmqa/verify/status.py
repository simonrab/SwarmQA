"""`<campaign>/verify/<verify_id>/status.json` and finding it again by id.

The file is a `VerifyResult` plus bookkeeping: `campaign_id`, `pid` (the
process doing the work), `phase` (`build`, `replay`, `done`), per-device
results under `device_results`, and timestamps. Writes are atomic
(temp file + rename), so a reader never sees half a file.

`start_verify` writes the runner's pid to `runner.pid` (before the runner
writes its own) and also drops `<index>/<verify_id>.json` pointing at the verify
directory, so `verify_status(verify_id)` works without a report root. The
index lives in `$AQA_VERIFY_INDEX`, else `~/.aqa/verify`.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

from swarmqa.mcp.tools import VerifyResult
from swarmqa.serialize import to_plain

STATUS_JSON = "status.json"
RUNNER_LOG = "runner.log"
RUNNER_PID = "runner.pid"
INDEX_ENV = "AQA_VERIFY_INDEX"
DEFAULT_INDEX = "~/.aqa/verify"
_RESULT_FIELDS = {item.name for item in fields(VerifyResult)}


def new_verify_id() -> str:
    """`v-<UTC timestamp>-<6 hex>`: sorts by start time and is unique across processes."""
    return f"v-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{secrets.token_hex(3)}"


def verify_dir(campaign_dir: Path, verify_id: str) -> Path:
    return Path(campaign_dir) / "verify" / Path(verify_id).name


def write_status(directory: Path, result: VerifyResult, **extra: Any) -> Path:
    """Write status.json atomically. `extra` keys are merged over what is already there."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATUS_JSON
    document = read_document(path) or {}
    document.update(to_plain(result))
    document.update(to_plain(extra))
    document["updated_at"] = time.time()
    temp = directory / f".{STATUS_JSON}.{os.getpid()}.{secrets.token_hex(2)}"
    temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


def read_document(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def result_from_document(document: dict[str, Any]) -> VerifyResult:
    kwargs = {key: value for key, value in document.items() if key in _RESULT_FIELDS}
    kwargs.setdefault("verify_id", "")
    kwargs.setdefault("finding_id", "")
    kwargs.setdefault("state", "error")
    return VerifyResult(**kwargs)


def read_pid(directory: Path) -> int | None:
    try:
        return int((Path(directory) / RUNNER_PID).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# Index ----------------------------------------------------------------------


def index_root() -> Path:
    return Path(os.environ.get(INDEX_ENV) or DEFAULT_INDEX).expanduser()


def record_index(verify_id: str, directory: Path) -> None:
    root = index_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        payload = {"verify_id": verify_id, "dir": str(Path(directory).resolve())}
        (root / f"{Path(verify_id).name}.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        return  # best effort: verify_status can still search a report root


def find_verify_dir(verify_id: str, report_root: str | Path | None = None) -> Path | None:
    """The directory holding `verify_id`'s status.json: the report root first, then the index, then ./reports."""
    name = Path(verify_id).name
    if not name:
        return None
    roots = [Path(report_root)] if report_root is not None else []
    for root in roots:
        found = _search(root, name)
        if found is not None:
            return found
    entry = read_document(index_root() / f"{name}.json")
    if entry and entry.get("dir"):
        candidate = Path(entry["dir"])
        if (candidate / STATUS_JSON).is_file():
            return candidate
    if report_root is None:
        return _search(Path("reports"), name)
    return None


def _search(root: Path, name: str) -> Path | None:
    if not root.is_dir():
        return None
    for campaign in sorted(root.iterdir(), reverse=True):
        candidate = campaign / "verify" / name
        if (candidate / STATUS_JSON).is_file():
            return candidate
    return None


# Liveness -------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """True when `pid` runs and is not a zombie."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        text = stat.read_text()
    except OSError:
        return True  # no /proc (macOS): kill(0) is all we have
    # Field 3, after the parenthesised command name.
    state = text.rsplit(")", 1)[-1].split()[:1]
    return state != ["Z"]
