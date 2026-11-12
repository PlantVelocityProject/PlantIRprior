#!/usr/bin/env bash
# See --help and docs/alignment-tool.md.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "${SCRIPT_DIR}/../containers/scripts/run_in_container.sh" --stage align_reads.py "$@"
