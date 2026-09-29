"""`aqa verify <finding-id>`: build, replay on N devices, and print the verdict.

Exit 0 when the finding did not reproduce on any device, 1 when it still
reproduces on at least one, 2 when it could not be verified (bad config,
unknown campaign or finding, build failure, or a device that could not run
the replay).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

EXIT_CODES = {"passed": 0, "failed": 1}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa verify", description="Rebuild the app and replay a finding.")
    parser.add_argument("finding_id", help="finding id from findings.json")
    parser.add_argument("--campaign", default=None, help="campaign id (default: the latest)")
    parser.add_argument("--devices", type=int, default=2, help="devices to replay on (default: 2)")
    parser.add_argument("--no-build", action="store_true", help="skip app.build_command")
    parser.add_argument("--config", default=None, help="config file (default: aqa.config.toml if present)")
    return parser


def main(argv: list[str] | None = None, *, driver_factory: Callable[[Any, Path], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from swarmqa.config import load_config_or_defaults
    from swarmqa.errors import ConfigError
    from swarmqa.verify.core import verify

    try:
        # A mistyped --config is an error: defaults have no build command, so
        # a verify on them would replay the old app and could report "passed".
        config = load_config_or_defaults(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    run = verify(
        args.finding_id,
        config=config,
        campaign_id=args.campaign,
        devices=args.devices,
        build=not args.no_build,
        driver_factory=driver_factory,
    )
    result = run.result
    for device in run.devices:
        verdict = {"reproduced": "reproduced", "clear": "did not reproduce"}.get(device.state, "could not replay")
        reason = device.reason.removeprefix("could not replay: ")
        print(f"{result.finding_id} on {device.device}: {verdict} ({reason})")
    if not run.devices or result.state == "error":
        print(f"{result.finding_id}: {result.state}: {result.message}", file=sys.stderr)
    if run.verify_dir is not None:
        print(f"verify {result.verify_id}: {result.state} ({run.verify_dir})", file=sys.stderr)
    return EXIT_CODES.get(result.state, 2)


if __name__ == "__main__":
    sys.exit(main())
