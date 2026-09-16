#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvo_schedule.core import MVOError, generate_full_list


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and verify mvo-full-list.txt")
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--source-command", nargs="+")
    parser.add_argument("--output", type=Path, default=Path("mvo-full-list.txt"))
    args = parser.parse_args()
    try:
        ids = generate_full_list(
            args.output, source_file=args.source_file, source_command=args.source_command
        )
    except MVOError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "cluster_count": len(ids)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
