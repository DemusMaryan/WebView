#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ -x "$CV_ROOT/build/exporter/mono_export" ]] || { echo 'Build the exporter first: bash scripts/cv/build-export-wsl.sh' >&2; exit 1; }
[[ -x "$CV_ROOT/venv/bin/python" ]] || { echo 'Prepare the plot venv: bash scripts/cv/setup-python-wsl.sh' >&2; exit 1; }
python3 "$CV_SCRIPTS/fetch-tum.py" "$CV_ROOT/datasets"
output="${1:-$CV_ROOT/runs/export-$(date -u +%Y%m%dT%H%M%SZ)}"
output="$(realpath -m -- "$output")"
case "$output" in /mnt/*|/) echo 'Run output must remain on the native WSL filesystem.' >&2; exit 1;; esac
mkdir -p -- "$(dirname -- "$output")"
mkdir -- "$output"
python3 "$CV_SCRIPTS/measure-export.py" "$ORB_ROOT" "$CV_ROOT/build/exporter/mono_export" \
    "$CV_ROOT/datasets/rgbd_dataset_freiburg1_xyz" "$output"
"$CV_ROOT/venv/bin/python" "$CV_SCRIPTS/analyze-run.py" "$output" --sequence "$CV_ROOT/datasets/rgbd_dataset_freiburg1_xyz"
printf 'Run output: %s\n' "$output"
