#!/usr/bin/env bash
# Run the generator via uv from anywhere; extra args are passed through (e.g. ./run.sh --check, ./run.sh --lan none).
set -euo pipefail
exec uv run --quiet --project "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" xml-gen-iterm2 "$@"
