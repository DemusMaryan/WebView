#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
python3 -m venv "$CV_ROOT/venv"
"$CV_ROOT/venv/bin/python" -m pip install --disable-pip-version-check -r "$CV_SCRIPTS/requirements.lock"
