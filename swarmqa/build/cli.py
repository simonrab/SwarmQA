"""`aqa build`: build a repo at a SHA for iOS Simulator and/or macOS.

    aqa build --sha <sha|ref> [--repo URL|PATH] [--platform ios|macos|all]
              [--project PATH] [--scheme NAME] [--config PATH] [--force] [--json]
    aqa build --prune [--keep N] [--max-age-days D] [--repo URL|PATH]

Prints one line per platform (`ios <app path> <bundle id>`), or with
`--json` one object with the artifacts and, for failures, the critical
findings. Exit 0 when every platform built, 1 when any failed, 2 on a usage
or config error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa build", description="Build a repo at a SHA.")
    parser.add_argument("--sha", help="commit SHA or ref to build")
    parser.add_argument("--repo", help="repo URL or local path (default: build.repo, then app.source_dir)")
    parser.add_argument("--platform", choices=("ios", "macos", "all"), default=None,
                        help="default: app.platform from the config, or all without a config")
    parser.add_argument("--project", help="project dir, .xcodeproj or .xcworkspace, relative to the checkout")
    parser.add_argument("--scheme", help="scheme to build (default: auto-detect)")
    parser.add_argument("--configuration", help="build configuration (default: Debug)")
    parser.add_argument("--config", default=None, help="config file (default: aqa.config.toml if present)")
    parser.add_argument("--force", action="store_true", help="rebuild even when a build of this SHA exists")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--prune", action="store_true", help="remove old checkouts and builds instead")
    parser.add_argument("--keep", type=int, default=None, help="with --prune: checkouts to keep (default: build.keep)")
    parser.add_argument("--max-age-days", type=float, default=None, help="with --prune: also remove older ones")
    return parser


def main(argv: list[str] | None = None, *, runner=None) -> int:
    args = build_parser().parse_args(argv)

    from swarmqa.build.builder import ShaBuilder
    from swarmqa.build.settings import BuildSettings
    from swarmqa.config import load_config_or_defaults
    from swarmqa.errors import ConfigError

    try:
        config = load_config_or_defaults(args.config)
        settings = BuildSettings.from_config(config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for key in ("repo", "project", "scheme", "configuration"):
        value = getattr(args, key)
        if value:
            setattr(settings, key, value)
    if not settings.repo:
        print("no repo: pass --repo or set build.repo (or app.source_dir)", file=sys.stderr)
        return EXIT_USAGE
    builder = ShaBuilder(settings, runner=runner)

    if args.prune:
        max_age = args.max_age_days * 86400 if args.max_age_days is not None else None
        removed = builder.prune(keep=args.keep, max_age_s=max_age)
        for path in removed:
            print(f"removed {path}")
        print(f"pruned {len(removed)} checkout(s)", file=sys.stderr)
        return EXIT_OK

    if not args.sha:
        print("--sha is required", file=sys.stderr)
        return EXIT_USAGE
    which = args.platform or (config.app.platform if args.config or _has_default_config() else "all")
    platforms = ("ios", "macos") if which == "all" else (which,)
    report = builder.build_many(args.sha, platforms, force=args.force)
    if args.json:
        document = {
            "sha": report.sha,
            "ok": report.ok,
            "results": [
                {"platform": item.platform, "ok": item.ok, "duration_s": item.duration_s,
                 "reused": item.reused, "mode": item.mode,
                 "artifact": asdict(item.artifact) if item.artifact else None,
                 "failure": asdict(item.failure) if item.failure else None}
                for item in report.results
            ],
            "findings": [asdict(finding) for finding in report.findings()],
        }
        print(json.dumps(document, indent=2))
    else:
        for item in report.results:
            if item.artifact is not None:
                note = " (cached)" if item.reused else f" ({item.duration_s:.1f}s)"
                print(f"{item.platform} {item.artifact.app_path} {item.artifact.bundle_id}{note}")
            elif item.failure is not None:
                print(f"{item.platform} FAILED: {item.failure.summary()}", file=sys.stderr)
    return EXIT_OK if report.ok else EXIT_FAILED


def _has_default_config() -> bool:
    from pathlib import Path

    from swarmqa.config import DEFAULT_CONFIG

    return Path(DEFAULT_CONFIG).is_file()


if __name__ == "__main__":
    sys.exit(main())
