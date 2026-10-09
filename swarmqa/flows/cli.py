"""`aqa flows`: propose intents from a change, and list the replay cache.

    aqa flows propose --base main [--head HEAD | --worktree] [--source DIR] [--out DIR]
    aqa flows list

Exit codes: 0 done, 2 usage, config or git error.
"""

from __future__ import annotations

import argparse
import sys

from swarmqa.errors import AQAError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa flows", description="Flows from the diff and the replay cache")
    sub = parser.add_subparsers(dest="action")
    propose = sub.add_parser("propose", help="Write intents for the flows a change puts at risk (needs [llm])")
    propose.add_argument("--base", required=True, help="Ref to compare against, e.g. main or origin/main")
    head = propose.add_mutually_exclusive_group()
    head.add_argument("--head", default="HEAD", help="Ref with the change (default: HEAD)")
    head.add_argument("--worktree", action="store_true", help="Compare with the working tree, uncommitted changes included")
    propose.add_argument("--source", help="Repo to read (default: app.source_dir, else .)")
    propose.add_argument("--out", default="intents/from-diff", help="Where to write the intents")
    propose.add_argument("--config", help="Config file (default: aqa.config.toml if present)")
    propose.add_argument("--max-flows", type=int, help="Override flows.max_flows")
    listing = sub.add_parser("list", help="List cached flows for the configured app")
    listing.add_argument("--config", help="Config file (default: aqa.config.toml if present)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 2
    from swarmqa.config import load_config_or_defaults

    try:
        config = load_config_or_defaults(args.config)
        if args.action == "propose":
            return _propose(args, config)
        return _list(config)
    except AQAError as exc:
        print(exc, file=sys.stderr)
        return 2


def _propose(args: argparse.Namespace, config) -> int:
    from swarmqa.flows.from_diff import intents_from_diff

    if args.max_flows is not None:
        config.flows.settings = {**config.flows.settings, "max_flows": args.max_flows}
    source = args.source or config.app.source_dir or "."
    head = None if args.worktree else args.head
    paths = intents_from_diff(config, source, args.base, head, args.out, log=lambda line: print(line, file=sys.stderr))
    for path in paths:
        print(path)
    return 0


def _list(config) -> int:
    from swarmqa.flows.cache import FlowCache

    cache = FlowCache.from_config(config, force=True)
    if cache is None:
        print("no flow cache: set app.source_dir or flows.cache_dir", file=sys.stderr)
        return 2
    entries = cache.entries()
    print(f"{cache.root}: {len(entries)} cached flow(s)")
    for flow in entries:
        print(
            f"  {flow.platform}/{flow.name}: {len(flow.steps)} step(s), runs {flow.runs}, "
            f"heals {flow.heals}, failures {flow.failures}, updated {flow.updated_at}"
        )
    return 0
