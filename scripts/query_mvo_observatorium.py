#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvo_schedule.core import MVOError, fetch_mvo_observatorium_ids, verify_ocm_production


def main() -> int:
    try:
        verify_ocm_production()
        cluster_ids = fetch_mvo_observatorium_ids()
    except MVOError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"clusters": cluster_ids}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
