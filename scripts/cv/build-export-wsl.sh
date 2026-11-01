#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
exec > >(tee "$CV_ROOT/logs/build-export-$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1
[[ "$(git -C "$ORB_ROOT" rev-parse HEAD)" == "$ORB_COMMIT" ]] || { echo 'Unexpected ORB-SLAM3 revision' >&2; exit 1; }
for library in "$ORB_ROOT/lib/libORB_SLAM3.so" "$ORB_ROOT/Thirdparty/DBoW2/lib/libDBoW2.so" \
               "$ORB_ROOT/Thirdparty/g2o/lib/libg2o.so" "$CV_ROOT/prefix/lib/libpango_core.so"; do
    [[ -f "$library" ]] || { echo "Prepared baseline dependency is missing: $library" >&2; exit 1; }
done
apply_cv_patch "$REPO_ROOT/patches/orbslam3-snapshot.patch"
# Only the patched core and thin driver need an incremental rebuild. Dependencies
# retain their existing sources, configuration and binaries.
cmake --build "$CV_ROOT/build/orbslam3" --target ORB_SLAM3 --parallel "$CV_JOBS"
cmake -S "$REPO_ROOT/native/cv" -B "$CV_ROOT/build/exporter" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DORB_ROOT="$ORB_ROOT" -DCMAKE_PREFIX_PATH="$CV_ROOT/prefix"
cmake --build "$CV_ROOT/build/exporter" --target mono_export --parallel "$CV_JOBS"
