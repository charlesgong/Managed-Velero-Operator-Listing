#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:---dry-run}" in
  --dry-run)
    action_flags=(--dry-run)
    ;;
  --execute)
    action_flags=(--yes)
    ;;
  *)
    echo "Usage: $0 [--dry-run|--execute]" >&2
    exit 2
    ;;
esac

exec osdctl servicelog post \
  --clusters-file "${script_dir}/mvo_clusters.json" \
  --template "${script_dir}/mvo_deprecation_servicelog.json" \
  "${action_flags[@]}"
