"""`aqa doctor`: check that this machine can run SwarmQA campaigns.

Each check returns a status (`ok`, `warn`, `fail`, or `skip`), a one-line
message and, when it is not `ok`, a fix hint. Every host command goes
through the `swarmqa.devices.commands` runner seam and every lookup
(`which`, environment, importable modules, platform) is injectable, so the
checks run on Linux in tests. Off macOS the Mac-only checks report
`skipped (not macOS)`. Key values are never printed, only variable names.
See docs/doctor.md.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import platform as _platform
import re
import shutil
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from swarmqa.devices.commands import Runner, default_runner, run

Status = Literal["ok", "warn", "fail", "skip"]

NOT_MACOS = "skipped (not macOS)"
MIN_PYTHON = (3, 11)
REQUIRED_GH_SCOPES = ("checks:write", "pull-requests:write")
_ARM_MACHINES = {"arm64", "aarch64", "arm64e"}
_EXTRAS = ("anthropic", "openai", "mcp")
_TIMEOUT = 30


@dataclass
class DoctorCheck:
    name: str
    status: Status
    message: str
    fix: str = ""


@dataclass
class DoctorEnv:
    """Everything a check reads from the host. Tests replace the fields."""

    runner: Runner = default_runner
    which: Callable[[str], str | None] = shutil.which
    platform: str = sys.platform
    machine: str = ""
    environ: Mapping[str, str] | None = None
    find_spec: Callable[[str], Any] = importlib.util.find_spec
    python_version: tuple[int, ...] = tuple(sys.version_info[:3])

    def __post_init__(self) -> None:
        if not self.machine:
            self.machine = _platform.machine()
        if self.environ is None:
            self.environ = os.environ

    @property
    def is_macos(self) -> bool:
        return self.platform == "darwin"


def run_doctor(
    config_path: Path | str | None = "aqa.config.toml",
    *,
    github: bool = False,
    env: DoctorEnv | None = None,
) -> list[DoctorCheck]:
    """Run every check and return them in display order."""
    env = env or DoctorEnv()
    config_check, config = check_config(config_path)
    checks = [
        check_python(env),
        check_xcode(env),
        check_simulators(env, config),
        check_idb(env),
        check_accessibility(env),
        check_ffmpeg(env),
        check_tart(env, config),
        check_llm_keys(env, config),
        *check_extras(env, config),
        config_check,
    ]
    if github:
        checks.append(check_github(env))
    return checks


def exit_code(checks: list[DoctorCheck]) -> int:
    """0 when nothing failed, 1 otherwise."""
    return 1 if any(check.status == "fail" for check in checks) else 0


def render_text(checks: list[DoctorCheck]) -> str:
    width = max((len(check.name) for check in checks), default=0)
    lines: list[str] = []
    for check in checks:
        lines.append(f"[{check.status:<4}] {check.name:<{width}}  {check.message}")
        if check.fix and check.status != "ok":
            lines.append(f"{'':>7}{'':<{width}}  fix: {check.fix}")
    counts = {status: sum(1 for c in checks if c.status == status) for status in ("ok", "warn", "fail", "skip")}
    lines.append("")
    lines.append(
        f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail, {counts['skip']} skipped"
    )
    return "\n".join(lines)


def render_json(checks: list[DoctorCheck]) -> str:
    payload = {"ok": exit_code(checks) == 0, "checks": [asdict(check) for check in checks]}
    return json.dumps(payload, indent=2)


# --- checks -----------------------------------------------------------------


def check_python(env: DoctorEnv) -> DoctorCheck:
    version = ".".join(str(part) for part in env.python_version)
    if tuple(env.python_version[:2]) >= MIN_PYTHON:
        return DoctorCheck("python", "ok", f"Python {version}")
    return DoctorCheck(
        "python",
        "fail",
        f"Python {version} is older than 3.11",
        "install with `uv tool install` so uv fetches a suitable Python",
    )


def check_xcode(env: DoctorEnv) -> DoctorCheck:
    if not env.is_macos:
        return DoctorCheck("xcode", "skip", NOT_MACOS)
    selected = run(env.runner, ["xcode-select", "-p"], timeout=_TIMEOUT)
    if not selected.ok:
        return DoctorCheck(
            "xcode",
            "fail",
            "no developer directory selected (xcode-select -p failed)",
            "install Xcode from the App Store, then `sudo xcode-select -s /Applications/Xcode.app`",
        )
    version = run(env.runner, ["xcodebuild", "-version"], timeout=_TIMEOUT)
    developer_dir = selected.stdout.strip()
    if not version.ok:
        return DoctorCheck(
            "xcode",
            "fail",
            f"xcodebuild is unavailable (developer dir {developer_dir})",
            "full Xcode is required, not only the Command Line Tools: "
            "`sudo xcode-select -s /Applications/Xcode.app`",
        )
    first = (version.stdout.strip().splitlines() or ["Xcode"])[0]
    return DoctorCheck("xcode", "ok", f"{first} at {developer_dir}")


def check_simulators(env: DoctorEnv, config: Any) -> DoctorCheck:
    if not env.is_macos:
        return DoctorCheck("simulators", "skip", NOT_MACOS)
    wants_ios = getattr(getattr(config, "app", None), "platform", "macos") == "ios"
    missing_status: Status = "fail" if wants_ios else "warn"
    runtimes_result = run(env.runner, ["xcrun", "simctl", "list", "runtimes", "-j"], timeout=_TIMEOUT)
    runtimes = _ios_runtimes(runtimes_result.stdout) if runtimes_result.ok else None
    if runtimes is None:
        return DoctorCheck(
            "simulators",
            missing_status,
            "could not list simulator runtimes (xcrun simctl list runtimes -j)",
            "open Xcode once to finish installing components, or run `xcodebuild -runFirstLaunch`",
        )
    if not runtimes:
        return DoctorCheck(
            "simulators",
            missing_status,
            "no available iOS simulator runtime",
            "install one in Xcode > Settings > Components, or `xcodebuild -downloadPlatform iOS`",
        )
    devices_result = run(
        env.runner, ["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=_TIMEOUT
    )
    devices = _ios_devices(devices_result.stdout) if devices_result.ok else []
    runtime_text = ", ".join(runtimes)
    if not devices:
        return DoctorCheck(
            "simulators",
            missing_status,
            f"runtimes: {runtime_text}; no available iOS devices",
            "create one in Xcode > Window > Devices and Simulators, or `xcrun simctl create`",
        )
    wanted = _configured_simulators(config)
    known = {name for name, _ in devices} | {udid for _, udid in devices}
    absent = [item for item in wanted if item not in known]
    if absent:
        return DoctorCheck(
            "simulators",
            missing_status,
            f"configured simulator not found: {', '.join(absent)}",
            "set app.simulator / app.simulators to a name from `xcrun simctl list devices available`",
        )
    return DoctorCheck(
        "simulators", "ok", f"runtimes: {runtime_text}; {len(devices)} available iOS devices"
    )


def check_idb(env: DoctorEnv) -> DoctorCheck:
    if not env.is_macos:
        return DoctorCheck("idb", "skip", NOT_MACOS)
    path = env.which("idb")
    if path:
        return DoctorCheck("idb", "ok", f"idb at {path}")
    return DoctorCheck(
        "idb",
        "warn",
        "idb not found (optional; the legacy iOS driver needs it for taps and the accessibility tree)",
        "brew tap facebook/fb && brew install facebook/fb/idb",
    )


_AX_SCRIPT = "ObjC.import('ApplicationServices'); $.AXIsProcessTrusted()"
_AX_FIX = (
    "System Settings > Privacy & Security > Accessibility: enable your terminal app "
    "(Terminal, iTerm, or the app that runs `aqa`), then restart it"
)


def check_accessibility(env: DoctorEnv) -> DoctorCheck:
    """Best effort: ask AXIsProcessTrusted through osascript, which inherits the terminal's grant."""
    if not env.is_macos:
        return DoctorCheck("accessibility", "skip", NOT_MACOS)
    result = run(env.runner, ["osascript", "-l", "JavaScript", "-e", _AX_SCRIPT], timeout=_TIMEOUT)
    answer = result.stdout.strip().lower()
    if result.ok and answer == "true":
        return DoctorCheck("accessibility", "ok", "Accessibility permission granted to this terminal")
    if result.ok and answer == "false":
        return DoctorCheck(
            "accessibility",
            "warn",
            "this terminal lacks Accessibility permission (needed to drive macOS apps)",
            _AX_FIX,
        )
    return DoctorCheck(
        "accessibility",
        "warn",
        "could not determine Accessibility permission",
        _AX_FIX,
    )


def check_ffmpeg(env: DoctorEnv) -> DoctorCheck:
    path = env.which("ffmpeg")
    if path:
        return DoctorCheck("ffmpeg", "ok", f"ffmpeg at {path}")
    return DoctorCheck(
        "ffmpeg",
        "warn",
        "ffmpeg not found (optional; used for video clips and frames)",
        "brew install ffmpeg",
    )


def check_tart(env: DoctorEnv, config: Any) -> DoctorCheck:
    if not env.is_macos:
        return DoctorCheck("tart", "skip", NOT_MACOS)
    wants_vm = getattr(config, "backend", "local") == "vm"
    missing_status: Status = "fail" if wants_vm else "warn"
    if env.machine.lower() not in _ARM_MACHINES:
        return DoctorCheck(
            "tart",
            missing_status,
            f"Tart VMs need Apple Silicon (this Mac is {env.machine or 'unknown'})",
            "use `backend = \"local\"` on Intel Macs",
        )
    tart_bin = getattr(getattr(config, "vm", None), "tart_bin", "tart") or "tart"
    path = env.which(tart_bin)
    if not path:
        return DoctorCheck(
            "tart",
            missing_status,
            "tart not found (optional; only `backend = \"vm\"` needs it)",
            "brew install cirruslabs/cli/tart (see docs/backends.md)",
        )
    version = run(env.runner, [tart_bin, "--version"], timeout=_TIMEOUT)
    detail = version.stdout.strip() if version.ok and version.stdout.strip() else path
    return DoctorCheck("tart", "ok", f"tart {detail} on Apple Silicon")


def check_llm_keys(env: DoctorEnv, config: Any) -> DoctorCheck:
    llm = getattr(config, "llm", None)
    if llm is None or llm.enabled is not True:
        return DoctorCheck("llm keys", "skip", "llm.enabled is false")
    try:
        from swarmqa.config import llm_settings

        settings = llm_settings(config)
    except (TypeError, ValueError) as exc:
        return DoctorCheck("llm keys", "fail", f"[llm] is invalid: {exc}", "fix the [llm] table")
    if settings.provider == "fake":
        return DoctorCheck("llm keys", "ok", "fake provider needs no key")
    name = settings.key_env()
    if (env.environ or {}).get(name, "").strip():
        return DoctorCheck("llm keys", "ok", f"{settings.provider}: {name} is set")
    return DoctorCheck(
        "llm keys",
        "fail",
        f"{settings.provider}: {name} is not set",
        f"export {name}=... in the shell or MCP client that runs `aqa`",
    )


def check_extras(env: DoctorEnv, config: Any) -> list[DoctorCheck]:
    llm = getattr(config, "llm", None)
    provider = None
    if llm is not None and llm.enabled is True:
        provider = llm.settings.get("provider", "anthropic")
    checks: list[DoctorCheck] = []
    for extra in _EXTRAS:
        name = f"extra: {extra}"
        try:
            present = env.find_spec(extra) is not None
        except (ImportError, ValueError):
            present = False
        if present:
            checks.append(DoctorCheck(name, "ok", f"{extra} is importable"))
            continue
        needed = extra == provider
        purpose = "`aqa mcp` needs it" if extra == "mcp" else f"the {extra} provider needs it"
        checks.append(
            DoctorCheck(
                name,
                "fail" if needed else "warn",
                f"{extra} is not installed ({purpose})",
                f"reinstall with the extra: uv tool install --reinstall "
                f"'swarmqa[{extra}] @ git+https://github.com/simonrab/swarmqa' (list every extra you use)",
            )
        )
    return checks


def check_config(config_path: Path | str | None) -> tuple[DoctorCheck, Any]:
    """Load the config quietly. Returns the check and the config (defaults when it cannot load)."""
    from swarmqa.config import load_config
    from swarmqa.errors import ConfigError
    from swarmqa.models import CampaignConfig

    if config_path is None:
        return DoctorCheck("config", "skip", "no config path"), CampaignConfig()
    path = Path(config_path)
    if not path.is_file():
        return (
            DoctorCheck("config", "warn", f"{path} not found", "run `aqa init` in the app repo"),
            CampaignConfig(),
        )
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            config = load_config(path)
    except ConfigError as exc:
        return (
            DoctorCheck(
                "config",
                "fail",
                f"{path} is invalid: {'; '.join(exc.errors)}",
                "fix the fields named above (see docs/config.md)",
            ),
            CampaignConfig(),
        )
    if _has_pr_table(path):
        return (
            DoctorCheck(
                "config",
                "warn",
                f"{path} loads, but its [pr] table is deprecated and ignored",
                "delete the [pr] table; coding agents open PRs over MCP (docs/agents.md)",
            ),
            config,
        )
    return DoctorCheck("config", "ok", f"{path} is valid"), config


def check_github(env: DoctorEnv) -> DoctorCheck:
    if not env.which("gh"):
        return DoctorCheck("github", "fail", "gh not found", "brew install gh && gh auth login")
    result = run(env.runner, ["gh", "auth", "status"], timeout=_TIMEOUT)
    # Older gh versions print the status on stderr.
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if not result.ok:
        return DoctorCheck("github", "fail", "gh is not logged in", "gh auth login")
    scopes = parse_gh_scopes(text)
    account = _gh_account(text)
    who = f" as {account}" if account else ""
    if scopes is None:
        return DoctorCheck(
            "github",
            "warn",
            f"gh is logged in{who}; token scopes are unknown",
            "make sure the token can write checks and pull requests "
            "(fine-grained: Checks and Pull requests read/write)",
        )
    missing = [scope for scope in REQUIRED_GH_SCOPES if scope not in scopes]
    if not missing:
        return DoctorCheck("github", "ok", f"gh logged in{who} with {', '.join(REQUIRED_GH_SCOPES)}")
    if "repo" in scopes:
        return DoctorCheck(
            "github",
            "warn",
            f"gh logged in{who} with classic 'repo' scope; {', '.join(missing)} not listed",
            "'repo' covers pull requests; creating check runs may need a GitHub App or "
            "fine-grained token with checks:write",
        )
    return DoctorCheck(
        "github",
        "fail",
        f"gh token{who} lacks {', '.join(missing)} (scopes: {', '.join(scopes) or 'none'})",
        "gh auth refresh -s repo, or use a token with checks:write and pull-requests:write",
    )


# --- parsing helpers ----------------------------------------------------------

_SCOPES_LINE = re.compile(r"Token scopes:\s*(.*)", re.IGNORECASE)
_ACCOUNT = re.compile(r"Logged in to \S+ (?:account|as) (\S+)", re.IGNORECASE)


def parse_gh_scopes(text: str) -> list[str] | None:
    """Scopes from the first `Token scopes:` line of `gh auth status`, or None when absent."""
    for line in text.splitlines():
        match = _SCOPES_LINE.search(line)
        if match:
            raw = match.group(1).strip()
            if raw.lower() in {"", "none"}:
                return []
            return [item.strip().strip("'\"") for item in raw.split(",") if item.strip().strip("'\"")]
    return None


def _gh_account(text: str) -> str | None:
    match = _ACCOUNT.search(text)
    return match.group(1).rstrip(")").lstrip("(") if match else None


def _ios_runtimes(stdout: str) -> list[str] | None:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return None
    names: list[str] = []
    for runtime in data.get("runtimes", []) if isinstance(data, dict) else []:
        if not isinstance(runtime, dict) or runtime.get("isAvailable") is False:
            continue
        platform_name = str(runtime.get("platform") or runtime.get("name") or "")
        identifier = str(runtime.get("identifier") or "")
        if platform_name.startswith("iOS") or ".iOS-" in identifier:
            names.append(str(runtime.get("name") or identifier))
    return names


def _ios_devices(stdout: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    found: list[tuple[str, str]] = []
    groups = data.get("devices", {}) if isinstance(data, dict) else {}
    for runtime, devices in groups.items() if isinstance(groups, dict) else []:
        if ".iOS-" not in str(runtime) and not str(runtime).startswith("iOS"):
            continue
        for device in devices or []:
            if isinstance(device, dict) and device.get("isAvailable", True) is not False:
                found.append((str(device.get("name", "")), str(device.get("udid", ""))))
    return found


def _configured_simulators(config: Any) -> list[str]:
    app = getattr(config, "app", None)
    if app is None or getattr(app, "platform", "macos") != "ios":
        return []
    wanted = list(getattr(app, "simulators", []) or [])
    if getattr(app, "simulator", None):
        wanted.insert(0, app.simulator)
    return list(dict.fromkeys(item for item in wanted if isinstance(item, str) and item))


def _has_pr_table(path: Path) -> bool:
    try:
        return "pr" in tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
