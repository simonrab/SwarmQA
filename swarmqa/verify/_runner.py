"""Detached entry point for `start_verify`: `python -m swarmqa.verify._runner <finding-id> ...`.

Runs `run_verify` with the given `--verify-id`, which writes the final
status.json. Output goes to `<verify dir>/runner.log`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swarmqa.verify._runner")
    parser.add_argument("finding_id")
    parser.add_argument("--config", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--devices", type=int, default=2)
    parser.add_argument("--verify-id", required=True)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--status-dir", help="status.json to finish if the run fails before writing its own")
    args = parser.parse_args(argv)

    from swarmqa.config import load_config
    from swarmqa.mcp.tools import VerifyResult
    from swarmqa.verify import run_verify
    from swarmqa.verify import status as status_mod

    try:
        config = load_config(Path(args.config))
        result = run_verify(
            args.finding_id,
            config=config,
            campaign_id=args.campaign,
            devices=args.devices,
            build=not args.no_build,
            verify_id=args.verify_id,
        )
    except Exception as exc:  # noqa: BLE001 - report every failure through status.json
        result = VerifyResult(args.verify_id, args.finding_id, "error", devices=args.devices, message=str(exc))
    if args.status_dir and result.state == "error":
        # A setup error returns before run_verify writes status.json; record the real reason.
        status_dir = Path(args.status_dir)
        current = status_mod.read_document(status_dir / status_mod.STATUS_JSON) or {}
        if current.get("state") == "running":
            status_mod.write_status(status_dir, result)
    print(f"verify {result.verify_id}: {result.state}\n{result.message}", flush=True)
    return 0 if result.state == "passed" else 1 if result.state == "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
