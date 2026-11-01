#!/usr/bin/env bash
set -euo pipefail
CV_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$CV_SCRIPTS/../.." && pwd)"
CV_ROOT="${CV_ROOT:-$HOME/.local/share/monotrajectoryweb}"
mkdir -p -- "$CV_ROOT"
CV_ROOT="$(realpath -- "$CV_ROOT")"
case "$CV_ROOT" in /mnt/*|/) echo 'CV_ROOT must be on the native WSL filesystem.' >&2; exit 1;; esac
ORB_ROOT="$CV_ROOT/src/ORB_SLAM3"
PANGO_ROOT="$CV_ROOT/src/Pangolin"
ORB_COMMIT=4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4
PANGO_COMMIT=aff6883c83f3fd7e8268a9715e84266c42e2efe3
CV_JOBS="${CV_JOBS:-2}"
mkdir -p -- "$CV_ROOT/src" "$CV_ROOT/build" "$CV_ROOT/logs" "$CV_ROOT/datasets" "$CV_ROOT/runs"

checkout_dependency() {
    local url="$1" directory="$2" commit="$3"
    if [[ ! -d "$directory/.git" ]]; then
        git init "$directory"
        git -C "$directory" remote add origin "$url"
        git -C "$directory" fetch --depth 1 origin "$commit"
        git -C "$directory" checkout --detach FETCH_HEAD
    fi
    [[ "$(git -C "$directory" rev-parse HEAD)" == "$commit" ]] || {
        echo "Unexpected dependency revision: $directory; inspect it manually." >&2; exit 1;
    }
}

apply_cv_patch() {
    apply_dependency_patch "$ORB_ROOT" "$1"
}

apply_dependency_patch() {
    local directory="$1" patch_file="$2"
    if git -C "$directory" apply --reverse --check "$patch_file" 2>/dev/null; then
        return
    fi
    git -C "$directory" apply --check "$patch_file"
    git -C "$directory" apply "$patch_file"
}
