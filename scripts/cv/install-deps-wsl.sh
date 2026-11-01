#!/usr/bin/env bash
set -euo pipefail
if [[ "$EUID" != 0 ]]; then
    echo 'Run this script with sudo in Ubuntu, or wsl -d Ubuntu -u root -- bash scripts/cv/install-deps-wsl.sh from the repository.' >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build git curl ca-certificates pkg-config \
    libopencv-dev libeigen3-dev libboost-serialization-dev \
    libglew-dev libgl1-mesa-dev libepoxy-dev libssl-dev \
    libjpeg-dev libpng-dev python3-venv
