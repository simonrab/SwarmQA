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


def main(argv: list[str] | None = None) -> int:
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
    run_p.add_argument("--pr-mode", choices=["off", "human", "autonomous"])
    run_p.add_argument("--resume", help="Campaign id whose pending and failed shards should rerun")
    run_p.add_argument("--reset-spend", action="store_true")
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
    replay_p.add_argument("--config", default="aqa.config.toml")
    replay_p.add_argument("--app", help="App to replay against (overrides app.path)")
    replay_p.set_defaults(func=cmd_replay)

    status_p = sub.add_parser("status", help="Show active campaign status")
    status_p.add_argument("--campaign")
    status_p.add_argument("--report-root", default="reports")
    status_p.set_defaults(func=cmd_status)
    return parser


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
        pr_mode=args.pr_mode,
        config_path=args.config,
    )
    config = load_config(Path(args.config), overrides)
    queue = build_queue(config)
    options = RunOptions(
        resume_campaign_id=args.resume,
        reset_spend=args.reset_spend,
        partial=bool(args.intents),
    )
    result = run_campaign(config, queue, options=options)
    if config.pr.mode in {"human", "autonomous"} and result.results:
        from swarmqa.prloop.loop import run_fix_loop

        loop = run_fix_loop(result, config, repo=Path.cwd())
        if loop.pr_url:
            print(loop.pr_url)
        elif loop.draft_path:
            print(loop.draft_path)
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

    from swarmqa.config import load_config
    from swarmqa.models import CampaignConfig
    from swarmqa.report.repro import replay_finding

    replay = Path(args.replay)
    if not replay.is_file():
        print(f"replay not found: {replay}", file=sys.stderr)
        return 2
    config_path = Path(args.config)
    config = load_config(config_path, CliOverrides(app=args.app)) if config_path.is_file() else CampaignConfig()
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
        pr_mode=getattr(args, "pr_mode", None),
        config_path=getattr(args, "config", None),
    )
