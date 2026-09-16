#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvo_schedule.core import (
    MVOError,
    collect_clusters,
    validate_collection,
    validate_external_ids,
    write_classification_csv,
    write_collection_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify MVO clusters and collect Sheet fields")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    try:
        ids = validate_external_ids(args.input.read_text(encoding="utf-8").splitlines())
        rows = collect_clusters(ids, workers=args.workers)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_classification_csv(args.output_dir / "mvo_sts_classification.csv", rows)
        write_collection_csv(args.output_dir / "mvo_cluster_list.csv", rows)
        validate_collection(rows, ids)
    except (OSError, MVOError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    non_sts = [row["external_id"] for row in rows if not row["sts_enabled"]]
    (args.output_dir / "non_sts_mvo_clusters.txt").write_text(
        "".join(f"{cluster_id}\n" for cluster_id in non_sts), encoding="utf-8"
    )
    print(json.dumps({"total": len(rows), "non_sts": len(non_sts)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
