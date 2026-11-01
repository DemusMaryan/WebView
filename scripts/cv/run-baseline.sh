#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
python3 "$CV_SCRIPTS/fetch-tum.py" "$CV_ROOT/datasets"
output="${1:-$CV_ROOT/runs/baseline-$(date -u +%Y%m%dT%H%M%SZ)}"
output="$(realpath -m -- "$output")"
case "$output" in /mnt/*|/) echo 'Run output must remain on the native WSL filesystem.' >&2; exit 1;; esac
mkdir -p -- "$(dirname -- "$output")"
mkdir -- "$output"  # Atomic reservation; never reuse or overwrite an earlier run.
python3 "$CV_SCRIPTS/measure-baseline.py" "$ORB_ROOT" "$CV_ROOT/datasets/rgbd_dataset_freiburg1_xyz" "$output"
