#!/usr/bin/env bash
# Kingfisher/qtransc entry point; see --help and docs/download-tool.md.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "${SCRIPT_DIR}/../containers/scripts/run_in_container.sh" --stage download_reads.py "$@"
