#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvo_schedule.cli import DEFAULT_SHEET_ID, DEFAULT_SHEET_RANGE
from mvo_schedule.core import (
    GoogleSheetsClient,
    MVOError,
    load_collection_artifacts,
    prepare_service_log_targets,
    sheet_values,
    update_sheet_verified,
    verify_ocm_production,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resume the verified Sheet/output phase of a collected MVO run"
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    parser.add_argument("--sheet-range", default=DEFAULT_SHEET_RANGE)
    parser.add_argument("--quota-project", required=True)
    args = parser.parse_args()
    try:
        verify_ocm_production()
        run_dir = args.run_dir.resolve()
        rows, cluster_path = load_collection_artifacts(run_dir)
        client = GoogleSheetsClient(
            args.sheet_id,
            args.sheet_range,
            quota_project=args.quota_project,
        )
        current = client.read()
        sheet_status = update_sheet_verified(
            client,
            sheet_values(rows),
            run_dir / "sheet_finalize_snapshot.json",
            write=True,
            current=current,
        )
        run_date = datetime.now().astimezone().strftime("%Y%m%d")
        targets = prepare_service_log_targets(
            rows,
            args.workdir.resolve() / "state" / "mvo_schedule.json",
            run_dir,
            run_date,
            commit_state=True,
        )
        result = {
            "status": "ok",
            "sheet": sheet_status,
            "cluster_count": len(rows),
            "stale_row_count": sum(1 for row in rows if row["status"] == "stale"),
            "mvo_cluster_list": str(cluster_path),
            "service_log": targets,
        }
        (run_dir / "result.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
    except MVOError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
