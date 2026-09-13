#!/usr/bin/env bash
# run_all.sh - start the whole TraceAI / SCAMNET project (Linux / macOS).
#
#   ./run_all.sh                backend + dashboard
#   ./run_all.sh --streamlit    + Streamlit UI
#   ./run_all.sh --reload       backend restarts on file changes
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python scripts/run_all.py "$@"
fi

exec python3 scripts/run_all.py "$@"
