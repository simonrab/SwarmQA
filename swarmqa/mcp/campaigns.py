"""Campaign bookkeeping behind the MCP tools.

The MCP server runs in a project directory (`root`) with one config file.
Campaigns it starts run in a detached `python -m swarmqa.mcp._runner`
process. `start_campaign` picks the campaign id up front, creates the
campaign directory, and writes `mcp.json` there with the request and the
runner's pid. The runner hands the id to the orchestrator through
`RunOptions.campaign_id`, which starts a fresh campaign under that id.

Cancelling writes `cancel-request.json`. The runner's clock honours it by
refusing to schedule new shards (drain); a hard cancel also sends SIGTERM to
the runner's process group.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarmqa.errors import AQAError
from swarmqa.models import CampaignConfig, CliOverrides, Finding

MCP_META = "mcp.json"
CANCEL_REQUEST = "cancel-request.json"
RUNNER_LOG = "raw/mcp-runner.log"
RUNNER_RESULT = "raw/mcp-runner.json"
DEFAULT_CONFIG = "aqa.config.toml"


@dataclass
class McpSettings:
    """Where the server looks for its config and resolves relative paths."""

    config_path: str = DEFAULT_CONFIG
    root: str | None = None


_settings = McpSettings()
_children: dict[int, subprocess.Popen] = {}


def configure(config_path: str | Path | None = None, root: str | Path | None = None) -> McpSettings:
    """Set the server's config path and project root (default: cwd)."""
    global _settings
    _settings = McpSettings(
        config_path=str(config_path) if config_path is not None else DEFAULT_CONFIG,
        root=str(Path(root).resolve()) if root is not None else None,
    )
    return _settings


def settings() -> McpSettings:
    return _settings


def project_root() -> Path:
    return Path(_settings.root) if _settings.root else Path.cwd()


def resolve_path(path: str | Path, base: Path | None = None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (base or project_root()) / candidate


def config_path(path: str | Path | None = None) -> Path:
    return resolve_path(path if path is not None else _settings.config_path)


def server_config() -> CampaignConfig:
    """The server's config, or defaults when the config file does not exist."""
    from swarmqa.config import load_config

    path = config_path()
    if not path.is_file():
        return CampaignConfig()
    return load_config(path)


def report_root(config: CampaignConfig | None = None) -> Path:
    config = config if config is not None else server_config()
    return resolve_path(config.report_root)


# -- campaign directories ----------------------------------------------------


def campaign_dirs(root: Path | None = None) -> list[Path]:
    """Campaign directories, oldest first (ids start with a UTC timestamp)."""
    root = root if root is not None else report_root()
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))


def campaign_path(campaign_id: str | None = None, root: Path | None = None) -> Path:
    """The campaign directory for `campaign_id`, or the latest campaign."""
    root = root if root is not None else report_root()
    if campaign_id:
        path = root / Path(campaign_id).name
        if not path.is_dir():
            raise AQAError(
                f"unknown campaign {campaign_id!r} under {root}; "
                "call campaign_status() without an id for the latest campaign"
            )
        return path
    dirs = campaign_dirs(root)
    if not dirs:
        raise AQAError(f"no campaigns in {root}; start one with start_campaign")
    return dirs[-1]


# -- metadata ------------------------------------------------------------------


def read_meta(campaign: Path) -> dict[str, Any]:
    path = Path(campaign) / MCP_META
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(campaign: Path, meta: dict[str, Any]) -> Path:
    path = Path(campaign) / MCP_META
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def update_meta(campaign: Path, **fields: Any) -> dict[str, Any]:
    meta = read_meta(campaign)
    meta.update(fields)
    write_meta(campaign, meta)
    return meta


@dataclass
class CampaignRequest:
    """What `start_campaign` asked for; the runner rebuilds the config from it."""

    config_path: str
    cwd: str
    app: str | None = None
    intents: list[str] | None = None
    platform: str | None = None
    workers: int | None = None
    sha: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_meta(cls, meta: dict[str, Any]) -> "CampaignRequest":
        request = dict(meta.get("request") or {})
        return cls(
            config_path=str(request.get("config_path") or DEFAULT_CONFIG),
            cwd=str(request.get("cwd") or os.getcwd()),
            app=request.get("app"),
            intents=request.get("intents"),
            platform=request.get("platform"),
            workers=request.get("workers"),
            sha=request.get("sha"),
        )

    def to_plain(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "cwd": self.cwd,
            "app": self.app,
            "intents": self.intents,
            "platform": self.platform,
            "workers": self.workers,
            "sha": self.sha,
        }


def load_request_config(request: CampaignRequest) -> CampaignConfig:
    """Load the config the way `aqa run` does, plus the MCP-only platform override.

    `report_root`, `intents` and `app.path` come back absolute (relative to
    the request's project root) so the runner and the server agree whatever
    their working directory.
    """
    from swarmqa.config import load_config, validate_config
    from swarmqa.errors import ConfigError

    base = Path(request.cwd)
    path = resolve_path(request.config_path, base)
    overrides = CliOverrides(
        app=request.app,
        intent=list(request.intents) if request.intents is not None else None,
        workers=request.workers,
        config_path=str(path),
    )
    config = load_config(path, overrides)
    if request.platform is not None:
        config.app.platform = request.platform  # type: ignore[assignment]
        errors = validate_config(config)
        if errors:
            raise ConfigError(errors)
    # `aqa run` resolves these against its cwd; the server's cwd may differ
    # from the project root, so pin them to the root here.
    config.report_root = str(resolve_path(config.report_root, base))
    config.intents = [str(resolve_path(item, base)) if str(item).strip() else item for item in config.intents]
    if config.app.path:
        config.app.path = str(resolve_path(config.app.path, base))
    return config


# -- runner process ------------------------------------------------------------


def launch_runner(campaign: Path, cwd: Path) -> int:
    """Start the detached runner for `campaign`; return its pid."""
    log_path = Path(campaign) / RUNNER_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # The runner imports this package from wherever the server imported it.
    package_parent = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = package_parent + (os.pathsep + existing if existing else "")
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "swarmqa.mcp._runner", str(campaign)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    _children[process.pid] = process
    return process.pid


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    child = _children.get(int(pid))
    if child is not None:
        return child.poll() is None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def runner_alive(meta: dict[str, Any]) -> bool:
    return pid_alive(meta.get("pid"))


def request_cancel(campaign: Path, *, drain: bool) -> Path:
    path = Path(campaign) / CANCEL_REQUEST
    payload = {"drain": bool(drain), "requested_at": now()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def cancel_requested(campaign: Path) -> bool:
    return (Path(campaign) / CANCEL_REQUEST).is_file()


def terminate_runner(pid: int, *, timeout_s: float = 5.0) -> bool:
    """SIGTERM the runner's process group, then SIGKILL it after `timeout_s`. True if it stopped."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(int(pid), sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            try:
                os.kill(int(pid), sig)
            except ProcessLookupError:
                return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.05)
    return not pid_alive(pid)


# -- findings --------------------------------------------------------------------


def load_campaign_findings(campaign: Path) -> list[Finding]:
    """Findings from `findings.json`; while a campaign runs, from worker results so far."""
    from swarmqa.report.findings_json import FINDINGS_JSON, load_findings_json

    path = Path(campaign) / FINDINGS_JSON
    if path.is_file():
        return load_findings_json(path)
    from swarmqa.serialize import load_json

    found: list[Finding] = []
    seen: set[str] = set()
    for result_path in sorted((Path(campaign) / "workers").glob("*/result.json")):
        try:
            data = load_json(result_path)
            findings = [Finding.from_dict(dict(item)) for item in data.get("findings", [])]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        for finding in findings:
            key = finding.fingerprint or finding.id
            if key in seen:
                continue
            seen.add(key)
            found.append(finding)
    return found


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
