#!/usr/bin/env bash
# Capability probe only: no jobs, CV, credentials or system configuration changes.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run this capability probe as WSL root.' >&2; exit 1; }
ROOT="${MTW_CONTAINER_ROOT:-/home/demus/.local/share/monotrajectoryweb/containers}"
case "$ROOT" in /mnt/*|/|*[[:space:]]*) echo 'Use a dedicated native WSL container directory.' >&2; exit 1;; esac
test -x /usr/bin/systemd-nspawn
mkdir -p "$ROOT/probe-rootfs"/{usr,etc,proc,sys,dev,run,tmp,var,home}
for name in bin sbin lib lib64; do
    test -e "$ROOT/probe-rootfs/$name" || ln -s "usr/$name" "$ROOT/probe-rootfs/$name"
done
printf 'ID=ubuntu\nNAME="MonoTrajectoryWeb namespace probe"\n' > "$ROOT/probe-rootfs/etc/os-release"
printf 'host-only\n' > "$ROOT/host-only-marker"
echo "HOST_PID_NAMESPACE=$(readlink /proc/self/ns/pid)"
echo "HOST_MOUNT_NAMESPACE=$(readlink /proc/self/ns/mnt)"
/usr/bin/systemd-nspawn --version | head -n 1
timeout --signal=TERM --kill-after=3 30 /usr/bin/systemd-nspawn \
    --directory="$ROOT/probe-rootfs" --machine=mtw-r7-probe --register=no --settings=no \
    --read-only --bind-ro=/usr:/usr --link-journal=no --timezone=off --resolv-conf=off \
    --console=pipe --private-users=no --as-pid2 \
    /bin/bash -c 'set -e; echo "CONTAINER_PID=$$"; echo "CONTAINER_PID_NAMESPACE=$(readlink /proc/self/ns/pid)"; echo "CONTAINER_MOUNT_NAMESPACE=$(readlink /proc/self/ns/mnt)"; ps -eo pid,comm; test ! -e /home/demus/.local/share/monotrajectoryweb/containers/host-only-marker; if touch /usr/mtw-probe-write 2>/dev/null; then echo "READ_ONLY_FAILED"; exit 1; fi; echo "FILESYSTEM_AND_PID_ISOLATION_OK"'
