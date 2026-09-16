from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import (
    GoogleSheetsClient,
    MVOError,
    acknowledge_pending,
    collect_clusters,
    generate_full_list,
    merge_collection_with_sheet,
    prepare_service_log_targets,
    sheet_values,
    update_sheet_verified,
    validate_collection,
    verify_ocm_production,
    write_classification_csv,
    write_collection_csv,
)


DEFAULT_SHEET_ID = "1OWOKfiegWe9IxE1PQyZZ3s8ZQ3hW6Veq6k5JyWW4Rh0"
DEFAULT_SHEET_RANGE = "MVO!A:I"


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MVOError(f"Cannot load config {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the scheduled MVO cluster scan")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--write-sheet", action="store_true", help="Update and verify the Google Sheet")
    parser.add_argument("--skip-sheet", action="store_true", help="Fixture/test runs only")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--ack-file", type=Path, help="Acknowledge IDs after a manual service-log post")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    workdir = args.workdir.resolve()
    state_path = workdir / "state" / "mvo_schedule.json"
    if args.ack_file:
        ids = [line.strip() for line in args.ack_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        remaining = acknowledge_pending(state_path, ids)
        return {"status": "acknowledged", "remaining_pending": remaining}

    config = load_config(args.config)
    identity = verify_ocm_production(timeout=int(config.get("command_timeout_seconds", 60)))
    stamp = datetime.now().astimezone()
    run_dir = workdir / "runs" / stamp.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    source_command = config.get("full_list_command")
    if source_command and (not isinstance(source_command, list) or not all(isinstance(x, str) for x in source_command)):
        raise MVOError("full_list_command must be a JSON array of command arguments")
    full_list_path = run_dir / "mvo-full-list.txt"
    external_ids = generate_full_list(
        full_list_path,
        source_file=args.source_file,
        source_command=source_command,
        timeout=int(config.get("command_timeout_seconds", 60)) * 2,
    )
    rows = collect_clusters(
        external_ids,
        workers=int(config.get("workers", 8)),
        timeout=int(config.get("command_timeout_seconds", 60)),
    )
    if args.skip_sheet:
        if args.write_sheet:
            raise MVOError("--skip-sheet and --write-sheet cannot be used together")
        current_sheet = None
        sheet_status = "skipped"
    else:
        client = GoogleSheetsClient(
            str(config.get("sheet_id", DEFAULT_SHEET_ID)),
            str(config.get("sheet_range", DEFAULT_SHEET_RANGE)),
            timeout=int(config.get("command_timeout_seconds", 60)),
            quota_project=str(config.get("google_quota_project", "")),
        )
        current_sheet = client.read()
        rows = merge_collection_with_sheet(rows, current_sheet)

    write_classification_csv(run_dir / "mvo_sts_classification.csv", rows)
    cluster_list_path = run_dir / f"mvo_cluster_list_{stamp.strftime('%Y%m%d')}.csv"
    write_collection_csv(cluster_list_path, rows)
    validate_collection(rows, external_ids)

    if not args.skip_sheet:
        sheet_status = update_sheet_verified(
            client,
            sheet_values(rows),
            run_dir / "sheet_snapshot.json",
            write=args.write_sheet,
            current=current_sheet,
        )

    # State moves only after an explicitly requested, verified Sheet write. A
    # skipped/read-only Sheet run produces inspectable targets without changing baseline.
    commit_state = bool(args.write_sheet and sheet_status == "updated")
    targets = prepare_service_log_targets(
        rows,
        state_path,
        run_dir,
        stamp.strftime("%Y%m%d"),
        commit_state=commit_state,
    )
    result = {
        "status": "ok",
        "ocm_identity_verified": bool(identity),
        "cluster_count": len(rows),
        "non_sts_count": sum(1 for row in rows if not row["sts_enabled"]),
        "stale_row_count": sum(1 for row in rows if row.get("status") == "stale"),
        "sheet": sheet_status,
        "state_committed": commit_state,
        "run_dir": str(run_dir),
        "mvo_cluster_list": str(cluster_list_path),
        "service_log": targets,
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run(args)
    except MVOError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
