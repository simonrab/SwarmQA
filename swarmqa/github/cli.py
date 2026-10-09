"""`aqa watch`: test PRs and default-branch merges as they appear.

    aqa watch [--config PATH] [--repo OWNER/NAME] [--once] [--interval S] [--dry-run]
    aqa watch --pr N | --latest-pr | --latest-merged

Without an event flag it polls until interrupted (`--once`: one poll).
Results print to stdout; they are posted to GitHub only when the config
sets `github.report`. Exit 0 when watching ends normally; with an event
flag, the event's exit code (0 clean, 1 findings or build failure, 2 could
not run).
"""

from __future__ import annotations

import argparse
import sys

EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa watch", description="Test GitHub PRs and merges.")
    parser.add_argument("--config", default=None, help="config file (default: aqa.config.toml if present)")
    parser.add_argument("--repo", help="owner/name (default: github.repo, issues.github_repo, then gh repo view)")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--pr", type=int, help="test this PR's head now")
    which.add_argument("--latest-pr", action="store_true", help="test the most recently updated open PR")
    which.add_argument("--latest-merged", action="store_true", help="test the latest PR merged into the branch")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--interval", type=float, help="seconds between polls (default: github.poll_interval_s)")
    parser.add_argument("--dry-run", action="store_true", help="print the events that would run; build nothing")
    return parser


def main(argv: list[str] | None = None, *, client=None, tester=None) -> int:
    args = build_parser().parse_args(argv)

    from swarmqa.config import load_config_or_defaults
    from swarmqa.errors import ConfigError
    from swarmqa.github.client import GitHubClient, GitHubError
    from swarmqa.github.settings import GitHubSettings
    from swarmqa.github.watch import Watcher, poll, pr_event, resolve_repo
    from swarmqa.github.events import Event

    try:
        config = load_config_or_defaults(args.config)
        settings = GitHubSettings.from_config(config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.repo:
        settings.repo = args.repo
    if client is None:
        repo = resolve_repo(config, settings)
        if not repo:
            print("no repo: pass --repo or set github.repo", file=sys.stderr)
            return EXIT_USAGE
        client = GitHubClient(repo)
    kwargs = {"tester": tester} if tester else {}
    watcher = Watcher(config, settings, client, **kwargs)
    try:
        event = None
        if args.pr is not None:
            event = pr_event(client.pull(args.pr))
        elif args.latest_pr:
            pull = client.latest_pull()
            if pull is None:
                print(f"{client.repo} has no open PRs", file=sys.stderr)
                return EXIT_USAGE
            event = pr_event(pull)
        elif args.latest_merged:
            branch = settings.branch or client.default_branch()
            pull = client.latest_merged(branch)
            if pull is None or not pull.merge_commit_sha:
                print(f"no merged PRs into {branch} in {client.repo}", file=sys.stderr)
                return EXIT_USAGE
            event = Event("push", pull.merge_commit_sha, None, branch, f"#{pull.number} {pull.title}", pull.url)
        if args.dry_run:
            if event is not None:
                events = [event]
            else:
                branch = settings.branch or client.default_branch()
                events = poll(client, settings, watcher.state, branch, save=False)
            for item in events:
                print(f"would test {item.label()} on {', '.join(settings.platforms)}"
                      + (f": {item.title}" if item.title else ""))
            if not events:
                print("nothing to test")
            return 0
        if event is not None:
            return watcher.process(event).exit_code
        return watcher.watch(once=args.once, interval=args.interval)
    except GitHubError as exc:
        print(f"github: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
