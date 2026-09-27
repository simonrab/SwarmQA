"""C8 — worker entrypoint used on every backend.

`python -m swarmqa.worker` reads a shard file and writes workers/<id>/result.json.
See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

import argparse
import sys

from swarmqa.errors import ChunkNotReady


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarmqa.worker")
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--shard-file", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.parse_args(argv)
    raise ChunkNotReady("C8", "swarmqa.worker")


if __name__ == "__main__":
    sys.exit(main())
