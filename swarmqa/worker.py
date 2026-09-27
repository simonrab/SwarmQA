"""C8 — worker entrypoint used on every backend.

`python -m swarmqa.worker` reads a shard file and writes workers/<id>/result.json.
See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from swarmqa.backends.local import execute_shard, make_error_result, terminate_all_live_procs
from swarmqa.models import CampaignConfig, Shard, WorkerResult
from swarmqa.orchestrator.status import model_from_plain
from swarmqa.report.layout import ensure_campaign_layout, worker_dir
from swarmqa.serialize import dump_json, load_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarmqa.worker")
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--shard-file", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    campaign_dir = Path(args.campaign_dir)
    result_path = campaign_dir / "workers" / args.worker_id / "result.json"
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _term_handler)
    try:
        shard = model_from_plain(Shard, load_json(Path(args.shard_file)))
        config = model_from_plain(CampaignConfig, load_json(Path(args.config_json)))
        # The parent already chose subprocess isolation. Stay in-process here.
        config.local.isolation = "thread"
        ensure_campaign_layout(campaign_dir)
        work = worker_dir(campaign_dir, args.worker_id)
        result = execute_shard(
            shard,
            args.worker_id,
            work,
            config,
            campaign_root=campaign_dir,
        )
        dump_json(result, result_path)
    except Exception as exc:
        wrote = _write_failure(campaign_dir, args.worker_id, exc)
        if not wrote:
            print(exc, file=sys.stderr)
            return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    return 0 if result_path.is_file() else 1


def _term_handler(signum: int, _frame) -> None:
    terminate_all_live_procs()
    os._exit(128 + signum)


def _write_failure(campaign_dir: Path, worker_id: str, exc: Exception) -> bool:
    try:
        work = campaign_dir / "workers" / worker_id
        work.mkdir(parents=True, exist_ok=True)
        shard = Shard(id="unknown", kind="suite", name="unknown")
        result: WorkerResult = make_error_result(shard, worker_id, str(exc), "local")
        dump_json(result, work / "result.json")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
