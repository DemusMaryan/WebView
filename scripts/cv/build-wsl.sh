#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
mode="${1:-baseline}"
[[ "$mode" == baseline ]] || { echo 'Usage: build-wsl.sh baseline (verified baseline build only)' >&2; exit 1; }
exec > >(tee "$CV_ROOT/logs/build-$mode-$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1
date -u +'%FT%TZ'
checkout_dependency https://github.com/UZ-SLAMLab/ORB_SLAM3.git "$ORB_ROOT" "$ORB_COMMIT"
checkout_dependency https://github.com/stevenlovegrove/Pangolin.git "$PANGO_ROOT" "$PANGO_COMMIT"

cmake -S "$PANGO_ROOT" -B "$CV_ROOT/build/pangolin" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$CV_ROOT/prefix" \
    '-DCMAKE_CXX_FLAGS=-include cstdint' \
    -DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON \
    -DBUILD_TOOLS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TESTS=OFF \
    -DBUILD_PANGOLIN_PYTHON=OFF -DBUILD_PANGOLIN_FFMPEG=OFF \
    -DBUILD_PANGOLIN_LIBDC1394=OFF -DBUILD_PANGOLIN_V4L=OFF \
    -DBUILD_PANGOLIN_OPENNI=OFF -DBUILD_PANGOLIN_OPENNI2=OFF \
    -DBUILD_PANGOLIN_REALSENSE=OFF -DBUILD_PANGOLIN_REALSENSE2=OFF
cmake --build "$CV_ROOT/build/pangolin" --parallel "$CV_JOBS"
cmake --install "$CV_ROOT/build/pangolin"

apply_cv_patch "$REPO_ROOT/patches/orbslam3-headless.patch"
apply_cv_patch "$REPO_ROOT/patches/orbslam3-cxx17.patch"
cmake -S "$ORB_ROOT/Thirdparty/DBoW2" -B "$CV_ROOT/build/dbow2" -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build "$CV_ROOT/build/dbow2" --parallel "$CV_JOBS"
[[ -f "$ORB_ROOT/Vocabulary/ORBvoc.txt" ]] || tar -xzf "$ORB_ROOT/Vocabulary/ORBvoc.txt.tar.gz" -C "$ORB_ROOT/Vocabulary"
cmake -S "$ORB_ROOT" -B "$CV_ROOT/build/orbslam3" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=17 -DCMAKE_CXX_STANDARD_REQUIRED=ON \
    -DCMAKE_PREFIX_PATH="$CV_ROOT/prefix" -DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON
cmake --build "$CV_ROOT/build/orbslam3" --target mono_tum --parallel "$CV_JOBS"

dpkg-query -W -f='${binary:Package}\t${Version}\n' \
    g++ cmake libopencv-dev libeigen3-dev libboost-serialization-dev \
    libglew-dev libgl1-mesa-dev libepoxy-dev libssl-dev libjpeg-dev libpng-dev \
    > "$CV_ROOT/logs/dependencies.tsv"
printf 'ORB_SLAM3\t%s\nPangolin\t%s\n' "$ORB_COMMIT" "$PANGO_COMMIT" >> "$CV_ROOT/logs/dependencies.tsv"
date -u +'%FT%TZ'
