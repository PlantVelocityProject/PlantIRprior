#!/usr/bin/env bash
# ENA discovery and sample catalog updates use the same pinned SIF as computation.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "${SCRIPT_DIR}/../containers/scripts/run_in_container.sh" --stage discover_samples.py "$@"
