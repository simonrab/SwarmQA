"""`aqa try`: crawl an app with no setup and open the report.

    aqa try [TARGET] [--scheme NAME] [--platform ios|macos] [--simulator NAME_OR_UDID]
            [--minutes N] [--goal TEXT ...] [--model] [--config PATH]
            [--report-root DIR] [--no-open] [--save]

TARGET is a built `.app`, an `.xcodeproj`/`.xcworkspace`, or a folder with
one (default: the current folder). Projects are built at HEAD with the
per-SHA builder. Contract: `main(argv) -> int`, called by `swarmqa.cli` with
every argument after `try`. Exit 0 when the campaign ran (findings or not),
2 when it could not run.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from swarmqa.errors import AQAError
from swarmqa.models import CampaignConfig, Shard
from swarmqa.quickstart.resolve import AppInfo, TryError

EXIT_OK, EXIT_CANNOT_RUN = 0, 2
SEVERITIES = ("critical", "high", "medium", "low")
MODEL_KEYS = (("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai"))
WALL_HEADROOM_S = 60.0
STEPS_PER_MINUTE = 40
REPO_URL = "https://github.com/simonrab/swarmqa"
USAGE = """no app or Xcode project here. Pass one, or run aqa try in a project folder.

Examples:
  aqa try path/to/MyApp.app              test a built app
  aqa try path/to/MyApp.xcodeproj        build the project, then test it
  cd MyProject && aqa try --scheme MyApp build and test from the project folder
"""


def _default_run(config: CampaignConfig, queue: list[Shard]):
    from swarmqa.orchestrator.campaign import run_campaign

    return run_campaign(config, queue)


def _default_builder(settings):
    from swarmqa.build.builder import ShaBuilder

    return ShaBuilder(settings)


def _default_html(campaign_dir: Path) -> Path | None:
    from swarmqa.report.html import write_html_report

    return write_html_report(campaign_dir)


def _default_runner(args: list[str], **kwargs: Any):
    from swarmqa.build.runner import build_runner

    return build_runner(args, **kwargs)


def _default_runner_state(platform: str) -> str:
    from swarmqa.quickstart.resolve import runner_state

    return runner_state(platform)


def _open(path: Path) -> None:
    subprocess.run(["open", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class TryEnv:
    """Everything `aqa try` touches on the host, replaceable in tests."""

    run_campaign: Callable[[CampaignConfig, list[Shard]], Any] = _default_run
    runner: Callable[..., Any] = _default_runner
    make_builder: Callable[[Any], Any] = _default_builder
    runner_state: Callable[[str], str] = _default_runner_state
    write_html: Callable[[Path], Path | None] = _default_html
    opener: Callable[[Path], None] = _open
    is_tty: Callable[[], bool] = lambda: sys.stdout.isatty()
    host: str = sys.platform
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    has_module: Callable[[str], bool] = lambda name: importlib.util.find_spec(name) is not None
    cwd: Path = field(default_factory=Path.cwd)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa try", description="Crawl an app with no setup and open the report.")
    parser.add_argument("target", nargs="?", help=".app, .xcodeproj, .xcworkspace or project folder")
    parser.add_argument("--scheme", help="scheme to build (default: auto-detect)")
    parser.add_argument("--platform", choices=("ios", "macos"), help="platform to build when both are possible")
    parser.add_argument("--simulator", help="iOS Simulator name or UDID (default: an available iPhone)")
    parser.add_argument("--minutes", type=float, default=5.0, help="time budget in minutes (default: 5)")
    parser.add_argument("--goal", action="append", default=[], help="a goal to try before the crawl (repeatable)")
    parser.add_argument("--model", action="store_true", help="use a model (needs ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    parser.add_argument("--config", help="start from this config instead of defaults")
    parser.add_argument("--report-root", default=None, help="where reports go (default: reports)")
    parser.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    parser.add_argument("--save", action="store_true", help="write a starter aqa.config.toml and intents/")
    return parser


def main(argv: list[str] | None = None, *, env: TryEnv | None = None) -> int:
    """Run `aqa try`. Returns 0 when the campaign ran, 2 when it could not."""
    env = env or TryEnv()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else EXIT_CANNOT_RUN
    try:
        return _try(args, env)
    except TryError as exc:
        print(f"aqa try: {exc}", file=env.err)
        return EXIT_CANNOT_RUN
    except KeyboardInterrupt:
        print("aqa try: stopped.", file=env.err)
        return EXIT_CANNOT_RUN


def _try(args: argparse.Namespace, env: TryEnv) -> int:
    if args.minutes <= 0:
        raise TryError("--minutes must be more than 0.")
    if env.host != "darwin":
        raise TryError("aqa try drives apps on a Mac. Run it on macOS with Xcode installed.")
    base = _load_base(args.config)
    app, plan = _locate(args, env, base)
    _check_runner(app.platform if app else plan.platform, base, env)
    if plan is not None:
        app = _build(plan, args, env, base)
    device, simulator = _device(app, args, env)
    config = make_config(base, app, minutes=args.minutes, goals=args.goal, simulator=simulator,
                         report_root=args.report_root)
    model_note = choose_model(config, args.model, env.environ, env.has_module)
    _validate(config)
    queue = make_queue(args.goal)
    if args.save:
        from swarmqa.quickstart.save import save_starter

        for line in save_starter(env.cwd, app, device if app.platform == "ios" else None):
            print(f"save: {line}", file=env.out)
    _print_plan(env.out, app, device, args, config, model_note)
    try:
        result = env.run_campaign(config, queue)
    except (AQAError, OSError) as exc:
        raise TryError(f"The run could not start: {exc}") from exc
    return _finish(result, args, env)


def _load_base(path: str | None) -> CampaignConfig | None:
    if not path:
        return None
    from swarmqa.config import load_config
    from swarmqa.errors import ConfigError

    try:
        return load_config(Path(path))
    except ConfigError as exc:
        raise TryError(f"config {path}: {exc}") from exc


def _locate(args: argparse.Namespace, env: TryEnv, base: CampaignConfig | None):
    """(app, None) for a built app, or (None, plan) for a project to build."""
    from swarmqa.quickstart.resolve import has_project, plan_project, read_app

    if args.target:
        target = Path(args.target).expanduser()
        if not target.is_absolute():
            target = env.cwd / target
        if not target.exists():
            raise TryError(f"{args.target} does not exist. Pass a built .app, a project, or a project folder.")
        if target.suffix == ".app":
            return _check_platform(read_app(target), args.platform), None
        return None, plan_project(target, env.runner, scheme=args.scheme, platform=args.platform)
    if base is not None and (base.app.path or base.app.bundle_id):
        path = Path(base.app.path).expanduser() if base.app.path else None
        if path is not None and path.suffix == ".app" and path.is_dir():
            app = read_app(path)
            app.source_dir = base.app.source_dir
            return app, None
        name = Path(base.app.path).stem if base.app.path else base.app.bundle_id or ""
        return AppInfo(base.app.platform, base.app.bundle_id or "", base.app.path, base.app.source_dir, name), None
    if has_project(env.cwd):
        return None, plan_project(env.cwd, env.runner, scheme=args.scheme, platform=args.platform)
    raise TryError(USAGE.rstrip())


def _check_platform(app: AppInfo, wanted: str | None) -> AppInfo:
    if wanted and wanted != app.platform:
        raise TryError(f"{app.name}.app is a {app.platform} app, not {wanted}. Drop --platform or pass "
                       f"a {wanted} build.")
    return app


def _check_runner(platform: str, base: CampaignConfig | None, env: TryEnv) -> None:
    if base is not None and base.driver.kind not in ("auto", "runner"):
        return
    state = env.runner_state(platform)
    if state == "build":
        print(f"note: the {platform} XCUITest runner is not built yet. SwarmQA builds it first "
              "(one time, a few minutes).", file=env.out)
    elif state == "missing":
        raise TryError(
            f"The {platform} XCUITest runner is not built. aqa try uses it to drive the app.\n"
            "Fix:\n"
            f"  1. git clone {REPO_URL} && cd swarmqa\n"
            f"  2. agents/swarm-runner/build.sh {platform}\n"
            "  3. Run aqa try again."
        )


def _build(plan, args: argparse.Namespace, env: TryEnv, base: CampaignConfig | None) -> AppInfo:
    from swarmqa.build.settings import BuildSettings
    from swarmqa.quickstart.resolve import build_app

    if plan.dirty:
        print("warning: the working tree has uncommitted changes. aqa try builds the last commit, "
              "so they are not included. Commit them to test them.", file=env.err)
    print(f"Building {plan.scheme} for {plan.platform} at commit {plan.sha[:10]}. "
          "The first build can take a few minutes.", file=env.out, flush=True)
    settings = BuildSettings.from_config(base) if base is not None else BuildSettings()
    return build_app(plan, settings, env.make_builder)


def _device(app: AppInfo, args: argparse.Namespace, env: TryEnv) -> tuple[str, str | None]:
    """(display name, value for app.simulator)."""
    if app.platform != "ios":
        return "this Mac", None
    from swarmqa.quickstart.resolve import pick_simulator

    udid, name = pick_simulator(env.runner, args.simulator)
    return name, udid


def make_config(base: CampaignConfig | None, app: AppInfo, *, minutes: float, goals: list[str],
                simulator: str | None = None, report_root: str | None = None) -> CampaignConfig:
    """The in-memory campaign config for one `aqa try` run."""
    config = base or CampaignConfig()
    config.app.path = app.path
    config.app.bundle_id = app.bundle_id
    config.app.platform = app.platform  # type: ignore[assignment]
    config.app.source_dir = app.source_dir or config.app.source_dir
    if simulator:
        config.app.simulator, config.app.simulators = simulator, []
    if base is None:
        config.workers = 1
        config.video.mode = "on_failure"
    if config.driver.kind == "auto":
        config.driver.kind = "runner"
    config.explorer.engine = "agent"
    seconds = minutes * 60
    config.explorer.max_time_s = seconds / (len(goals) + 1)
    config.explorer.max_steps = max(config.explorer.max_steps, int(minutes * STEPS_PER_MINUTE))
    config.budgets.max_wall_time_s = seconds + WALL_HEADROOM_S
    config.intents = []
    if report_root:
        config.report_root = report_root
    return config


def choose_model(config: CampaignConfig, wanted: bool, environ: Mapping[str, str],
                 has_module: Callable[[str], bool]) -> str:
    """Turn on `[llm]` for `--model` when an API key and the SDK are present. Returns a line to print."""
    if not wanted:
        if config.llm.enabled:
            return f"Model: {config.llm.settings.get('provider', 'anthropic')} (from your config). Model calls cost money."
        return "Model: none (free). The crawl uses built-in rules. Add --model to use a model."
    for key, provider in MODEL_KEYS:
        if not environ.get(key):
            continue
        if not has_module(provider):
            return (f"Model: none (free). --model needs the {provider} package: "
                    f"uv tool install 'swarmqa[{provider}]'.")
        config.llm.enabled = True
        config.llm.settings = {**config.llm.settings, "provider": provider}
        config.llm.settings.pop("api_key_env", None)
        return f"Model: {provider} (key from {key}). Model calls cost money."
    return "Model: none (free). --model needs ANTHROPIC_API_KEY or OPENAI_API_KEY in the environment."


def make_queue(goals: list[str]) -> list[Shard]:
    """One goal shard per `--goal`, then one crawl shard (no goal)."""
    shards = [
        Shard(id=f"s-goal-{index}", kind="exploratory", name=f"goal {index}", goal=goal.strip(), tags=["try"])
        for index, goal in enumerate((goal for goal in goals if goal.strip()), start=1)
    ]
    return [*shards, Shard(id="s-crawl", kind="exploratory", name="crawl", tags=["try"])]


def _validate(config: CampaignConfig) -> None:
    from swarmqa.config import validate_config

    errors = validate_config(config)
    if errors:
        raise TryError("the run settings are not valid: " + "; ".join(errors))


def _print_plan(out: TextIO, app: AppInfo, device: str, args: argparse.Namespace,
                config: CampaignConfig, model_note: str) -> None:
    goals = [goal for goal in args.goal if goal.strip()]
    lines = [
        "",
        f"aqa try will test {app.name or app.bundle_id} ({app.bundle_id}).",
        f"  1. Launch it on {device} ({app.platform}).",
    ]
    if goals:
        lines.append(f"  2. Try {len(goals)} goal(s), then crawl the app and check every screen.")
    else:
        lines.append("  2. Crawl the app and check every screen.")
    lines += [
        f"  3. Stop after about {args.minutes:g} minute(s) and write the report under {config.report_root}/.",
        model_note,
        "",
    ]
    print("\n".join(lines), file=out, flush=True)


def _finish(result: Any, args: argparse.Namespace, env: TryEnv) -> int:
    results = list(getattr(result, "results", []) or [])
    report_dir = Path(result.report_dir)
    if results and all(item.status == "error" for item in results):
        first = next((item.error for item in results if item.error), "unknown error")
        print(f"aqa try: the app could not be tested: {first}", file=env.err)
        print(f"Details: {report_dir}", file=env.err)
        return EXIT_CANNOT_RUN
    html = _html(report_dir, env)
    counts = _severity_counts(results)
    if counts:
        parts = [f"{counts[name]} {name}" for name in SEVERITIES if counts.get(name)]
        summary = f"Found {sum(counts.values())} problem(s): {', '.join(parts)}."
    else:
        summary = "No problems found."
    page = html or report_dir / "summary.md"
    lines = ["", summary, f"Report: {page}", "", "Next steps:"]
    if not args.save:
        lines.append("  aqa try --save   write aqa.config.toml and an example intent to edit")
    lines.append("  aqa run          run the saved config (your intents plus the crawl)")
    print("\n".join(lines), file=env.out)
    if html is not None and not args.no_open and env.is_tty():
        env.opener(html)
    return EXIT_OK


def _html(report_dir: Path, env: TryEnv) -> Path | None:
    try:
        return env.write_html(report_dir)
    except Exception as exc:  # noqa: BLE001 - the report must not fail the run
        print(f"warning: HTML report failed: {exc}", file=env.err)
        return None


def _severity_counts(results: list[Any]) -> Counter:
    seen: dict[str, str] = {}
    for item in results:
        for finding in item.findings:
            seen.setdefault(finding.fingerprint or finding.id, finding.severity)
    return Counter(seen.values())
