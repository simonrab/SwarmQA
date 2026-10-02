"""Command line entry for Autonomous QA.

This dispatcher is shared. Feature chunks implement the functions it calls.
They leave this file alone so campaigns gain behavior as modules land.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from swarmqa import __version__
from swarmqa.errors import AQAError, ChunkNotReady, ConfigError
from swarmqa.models import CliOverrides

_TRACKERS = ("github", "linear")


_PASSTHROUGH = {"mcp": "cmd_mcp", "verify": "cmd_verify", "build": "cmd_build", "watch": "cmd_watch"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _PASSTHROUGH:
        # These parse their own flags, including --help.
        return int(globals()[_PASSTHROUGH[argv[0]]](argparse.Namespace(rest=argv[1:])))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except ChunkNotReady as exc:
        print(exc, file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"not implemented: {exc}", file=sys.stderr)
        return 2
    except AQAError as exc:
        print(exc, file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa", description="Autonomous QA campaigns")
    parser.add_argument("--version", action="version", version=f"aqa {__version__}")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="Write aqa.config.toml and report conventions")
    init_p.add_argument("--dir", default=".", help="Project directory")
    init_p.set_defaults(func=cmd_init)

    run_p = sub.add_parser("run", help="Run a campaign")
    run_p.add_argument("--config", default="aqa.config.toml")
    run_p.add_argument("--app")
    run_p.add_argument("--intent", action="append", dest="intents")
    run_p.add_argument("--backend", choices=["local", "vm", "cloud"])
    run_p.add_argument("--workers", type=int)
    run_p.add_argument("--max-wall-time")
    run_p.add_argument("--max-spend", type=float)
    run_p.add_argument("--spend-currency")
    run_p.add_argument("--video-mode", choices=["always", "on_failure", "exploratory_only"])
    run_p.add_argument("--resume", help="Campaign id whose pending and failed shards should rerun")
    run_p.add_argument("--reset-spend", action="store_true")
    run_p.add_argument(
        "--github-sha",
        help="Build this commit, run one campaign per github.platforms, and report it (see docs/github.md)",
    )
    run_p.add_argument("--github-pr", type=int, help="With --github-sha: the PR to comment on (default: looked up)")
    run_p.add_argument("--clone-url", help="With --github-sha: repo URL or path to build from (overrides github.clone_url)")
    run_p.set_defaults(func=cmd_run)

    rec_p = sub.add_parser("record", help="Write a JSON recorded flow")
    rec_p.add_argument("--app", required=True)
    rec_p.add_argument("--out", required=True)
    rec_p.add_argument("--name", default="recorded-flow")
    rec_p.add_argument("--interactive", action="store_true")
    rec_p.set_defaults(func=cmd_record)

    report_p = sub.add_parser("report", help="Print the latest campaign summary")
    report_p.add_argument("--campaign")
    report_p.add_argument("--report-root", default="reports")
    report_p.set_defaults(func=cmd_report)

    base_p = sub.add_parser("baseline", help="Baseline commands")
    base_sub = base_p.add_subparsers(dest="baseline_command")
    update_p = base_sub.add_parser("update", help="Promote current screenshots to baselines")
    update_p.add_argument("--baseline-dir", default="baselines")
    update_p.add_argument("--from-dir", required=True)
    update_p.set_defaults(func=cmd_baseline_update)
    base_p.set_defaults(func=lambda args: _baseline_help(base_p, args))

    replay_p = sub.add_parser(
        "replay",
        help="Replay a finding and report whether it still reproduces (exit 1 if it does)",
    )
    replay_p.add_argument("replay", help="Path to findings/<id>.replay.json")
    replay_p.add_argument("--config", help="Config file (default: aqa.config.toml if present)")
    replay_p.add_argument("--app", help="App to replay against (overrides app.path)")
    replay_p.set_defaults(func=cmd_replay)

    status_p = sub.add_parser("status", help="Show active campaign status")
    status_p.add_argument("--campaign")
    status_p.add_argument("--report-root", default="reports")
    status_p.set_defaults(func=cmd_status)

    issues_p = sub.add_parser(
        "file-issues",
        help="File a campaign's findings as tracker issues (dry run unless --yes)",
    )
    issues_p.add_argument("--campaign", help="Campaign id (default: the latest)")
    issues_p.add_argument("--report-root", default="reports")
    issues_p.add_argument("--config", help="Config file (default: aqa.config.toml if present)")
    issues_p.add_argument(
        "--tracker",
        action="append",
        dest="trackers",
        choices=list(_TRACKERS),
        help="Tracker to file into (repeatable). Default: the ones enabled in [issues]",
    )
    issues_p.add_argument(
        "--skip-advisory", action="store_true", help="Leave out model-only (advisory) findings"
    )
    issues_p.add_argument("--yes", action="store_true", help="Actually file; without it, only print the plan")
    issues_p.set_defaults(func=cmd_file_issues)

    doctor_p = sub.add_parser("doctor", help="Check Xcode, simulators, permissions, keys and tools")
    doctor_p.add_argument("--config", default="aqa.config.toml")
    doctor_p.add_argument("--github", action="store_true", help="Also check gh auth and token scopes")
    doctor_p.add_argument("--json", action="store_true", help="Print the checks as JSON")
    doctor_p.set_defaults(func=cmd_doctor)

    # These two parse their own flags; the rest of argv is handed over as is.
    mcp_p = sub.add_parser("mcp", help="Run the MCP server over stdio (needs swarmqa[mcp])", add_help=False)
    mcp_p.add_argument("rest", nargs=argparse.REMAINDER)
    mcp_p.set_defaults(func=cmd_mcp)

    verify_p = sub.add_parser(
        "verify",
        help="Rebuild and replay a finding on N devices (exit 0 fixed, 1 reproduces, 2 could not verify)",
        add_help=False,
    )
    verify_p.add_argument("rest", nargs=argparse.REMAINDER)
    verify_p.set_defaults(func=cmd_verify)

    build_p = sub.add_parser("build", help="Check out a SHA and build its iOS/macOS apps", add_help=False)
    build_p.add_argument("rest", nargs=argparse.REMAINDER)
    build_p.set_defaults(func=cmd_build)

    watch_p = sub.add_parser("watch", help="Test GitHub PRs and default-branch merges as they appear", add_help=False)
    watch_p.add_argument("rest", nargs=argparse.REMAINDER)
    watch_p.set_defaults(func=cmd_watch)
    return parser


def cmd_mcp(args: argparse.Namespace) -> int:
    from swarmqa.mcp.server import main as mcp_main

    return mcp_main(args.rest)


def cmd_verify(args: argparse.Namespace) -> int:
    from swarmqa.verify.cli import main as verify_main

    return verify_main(args.rest)


def cmd_build(args: argparse.Namespace) -> int:
    from swarmqa.build.cli import main as build_main

    return build_main(args.rest)


def cmd_watch(args: argparse.Namespace) -> int:
    from swarmqa.github.cli import main as watch_main

    return watch_main(args.rest)


def cmd_init(args: argparse.Namespace) -> int:
    destination = Path(args.dir)
    destination.mkdir(parents=True, exist_ok=True)
    template_root = Path(__file__).resolve().parent / "templates"
    config_path = destination / "aqa.config.toml"
    template = (template_root / "aqa.config.toml").read_text(encoding="utf-8")
    issue = (template_root / "issue.md").read_text(encoding="utf-8")
    if not config_path.exists():
        config_path.write_text(template, encoding="utf-8")
    issue_path = destination / "templates" / "issue.md"
    if not issue_path.exists():
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(issue, encoding="utf-8")
    intents = destination / "intents"
    intents.mkdir(parents=True, exist_ok=True)
    gitkeep = intents / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")
    reports = destination / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    print(f"wrote {config_path}")
    print(f"wrote {issue_path}")
    print(f"intents directory: {intents}")
    print(f"reports directory: {reports}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from swarmqa.config import load_config
    from swarmqa.intent.ingest import build_queue
    from swarmqa.models import RunOptions
    from swarmqa.orchestrator.campaign import run_campaign

    overrides = CliOverrides(
        app=args.app,
        intent=args.intents,
        backend=args.backend,
        workers=args.workers,
        max_wall_time=args.max_wall_time,
        max_spend=args.max_spend,
        spend_currency=args.spend_currency,
        video_mode=args.video_mode,
        config_path=args.config,
    )
    config = load_config(Path(args.config), overrides)
    if args.github_sha:
        from swarmqa.github.watch import run_sha

        return run_sha(config, args.github_sha, pr=args.github_pr, clone_url=args.clone_url)
    if args.github_pr or args.clone_url:
        raise ConfigError(["--github-pr and --clone-url need --github-sha"])
    queue = build_queue(config)
    options = RunOptions(
        resume_campaign_id=args.resume,
        reset_spend=args.reset_spend,
        partial=bool(args.intents),
    )
    result = run_campaign(config, queue, options=options)
    print(result.report_dir)
    return result.exit_code


def cmd_record(args: argparse.Namespace) -> int:
    from swarmqa.intent.record import record_flow

    path = record_flow(
        app_path=args.app,
        out_path=Path(args.out),
        name=args.name,
        interactive=args.interactive or not sys.stdin.isatty(),
    )
    print(path)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.report_root)
    campaign = _resolve_campaign(root, args.campaign)
    summary = campaign / "summary.md"
    if not summary.exists():
        print(f"no summary at {summary}", file=sys.stderr)
        return 2
    print(summary.read_text(encoding="utf-8"))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Exit 0 when the finding no longer reproduces, 1 when it does, 2 when the replay could not run."""
    import time

    from swarmqa.config import load_config_or_defaults
    from swarmqa.report.repro import replay_finding

    replay = Path(args.replay)
    if not replay.is_file():
        print(f"replay not found: {replay}", file=sys.stderr)
        return 2
    config = load_config_or_defaults(args.config)
    if args.app:
        config.app.path = args.app
    # <campaign>/findings/<id>.replay.json -> <campaign>/replays/<id>-<time>
    campaign = replay.resolve().parent.parent
    stamp = time.strftime("%Y%m%dT%H%M%S")
    work_dir = campaign / "replays" / f"{replay.name.removesuffix('.replay.json')}-{stamp}"
    result = replay_finding(replay, config, work_dir=work_dir)
    if not result.ran:
        print(f"{result.finding_id or replay.name}: could not replay ({result.reason})", file=sys.stderr)
        return 2
    verdict = "reproduced" if result.reproduced else "did not reproduce"
    print(f"{result.finding_id or replay.name}: {verdict} ({result.reason})")
    return 1 if result.reproduced else 0


def cmd_status(args: argparse.Namespace) -> int:
    from swarmqa.orchestrator.status import read_status

    status = read_status(Path(args.report_root), args.campaign)
    print(status)
    return 0


def cmd_file_issues(args: argparse.Namespace) -> int:
    """Dry run by default: print what would be filed. `--yes` files and prints each ref."""
    from swarmqa.config import load_config_or_defaults
    from swarmqa.report.findings_json import FINDINGS_JSON, load_findings_json

    config = load_config_or_defaults(args.config)
    campaign = _resolve_campaign(Path(args.report_root), args.campaign)
    findings_path = campaign / FINDINGS_JSON
    if not findings_path.is_file():
        print(f"no findings.json at {findings_path}", file=sys.stderr)
        return 2
    if args.trackers is not None:
        config.issues.github = "github" in args.trackers
        config.issues.linear = "linear" in args.trackers
    findings = load_findings_json(findings_path)
    if args.skip_advisory:
        findings = [finding for finding in findings if not finding.advisory]
    trackers = [name for name in ("github", "linear") if getattr(config.issues, name)]
    problems = _tracker_problems(config, trackers)
    targets = ", ".join(["local", *trackers])
    print(f"campaign {campaign.name}: {len(findings)} finding(s) -> {targets}")
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    if not args.yes:
        for finding in findings:
            advisory = " (advisory)" if finding.advisory else ""
            print(f"  would file {finding.id} [{finding.severity}] {finding.title}{advisory}")
        print("dry run: nothing filed. Re-run with --yes to file.")
        return 0

    from swarmqa.reporter.issues import create_issues, json_post

    refs = create_issues(findings, config, campaign, http_post=json_post)
    for ref in refs:
        location = ref.url or ref.identifier
        print(f"  {ref.finding_id} -> {ref.tracker}: {location}")
    # Every enabled tracker must have filed every finding; one tracker's
    # success does not cover another's failure.
    filed = {(ref.finding_id, ref.tracker) for ref in refs}
    missing = [(f.id, name) for f in findings for name in trackers if (f.id, name) not in filed]
    if missing:
        for finding_id, name in missing:
            print(f"  {finding_id} -> {name}: not filed", file=sys.stderr)
        print(
            f"{len(missing)} filing(s) failed; see findings/<id>.md for the error",
            file=sys.stderr,
        )
        return 1
    return 0


def _tracker_problems(config, trackers: list[str]) -> list[str]:
    import os

    problems: list[str] = []
    if "github" in trackers and not config.issues.github_repo:
        problems.append("issues.github_repo is not set; GitHub filing will fail")
    if "linear" in trackers:
        if not config.issues.linear_team:
            problems.append("issues.linear_team is not set; Linear filing will fail")
        key = config.issues.linear_api_key_env or "LINEAR_API_KEY"
        if not os.environ.get(key):
            problems.append(f"{key} is not set; Linear filing will fail")
    return problems


def cmd_doctor(args: argparse.Namespace) -> int:
    from swarmqa.doctor import exit_code, render_json, render_text, run_doctor

    checks = run_doctor(Path(args.config), github=args.github)
    print(render_json(checks) if args.json else render_text(checks))
    return exit_code(checks)


def cmd_baseline_update(args: argparse.Namespace) -> int:
    from swarmqa.visual.baseline import update_baselines

    written = update_baselines(Path(args.from_dir), Path(args.baseline_dir))
    for path in written:
        print(path)
    return 0


def _baseline_help(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if getattr(args, "baseline_command", None):
        return 2
    parser.print_help()
    return 2


def _resolve_campaign(root: Path, campaign_id: str | None) -> Path:
    if campaign_id:
        return root / campaign_id
    if not root.exists():
        raise ConfigError([f"report root not found: {root}"])
    campaigns = sorted(path for path in root.iterdir() if path.is_dir())
    if not campaigns:
        raise ConfigError([f"no campaigns in {root}"])
    return campaigns[-1]


def overrides_from_namespace(args: argparse.Namespace) -> CliOverrides:
    return CliOverrides(
        app=getattr(args, "app", None),
        intent=getattr(args, "intents", None),
        backend=getattr(args, "backend", None),
        workers=getattr(args, "workers", None),
        max_wall_time=getattr(args, "max_wall_time", None),
        max_spend=getattr(args, "max_spend", None),
        spend_currency=getattr(args, "spend_currency", None),
        video_mode=getattr(args, "video_mode", None),
        config_path=getattr(args, "config", None),
    )
