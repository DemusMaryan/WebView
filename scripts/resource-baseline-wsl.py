"""Passive 60-second WSL/Windows cross-check; never starts SLAM or a helper."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import time
from mtw.capacity import ensure_space


def windows_path(value):
    p = PureWindowsPath(value)
    return Path("/mnt") / p.drive[0].lower() / Path(*p.parts[1:])


def latest_windows(path):
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 65536))
        lines = f.read().splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
            if row.get("type") == "sample":
                return row
        except (ValueError, UnicodeError):
            pass
    raise ValueError("No complete Windows sample")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    context = json.loads(args.context.read_text())
    winlog = windows_path(context["windows_log"])
    events = []
    sys.addaudithook(lambda event, values: events.append(event)
                    if event in {"subprocess.Popen", "os.system", "os.posix_spawn"} else None)
    start = time.monotonic()
    with args.output.open("x", buffering=1) as out:
        for n in range(61):
            time.sleep(max(0, start + n - time.monotonic()))
            before = time.perf_counter()
            error = None
            try:
                ensure_space()
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            latency = time.perf_counter() - before
            stat_error = None
            stat_values = free_bytes = None
            try:
                st = os.statvfs("/mnt/c")
                stat_values = {"f_bavail": st.f_bavail, "f_bfree": st.f_bfree, "f_frsize": st.f_frsize}
                free_bytes = st.f_bavail * st.f_frsize
            except OSError as e:
                stat_error = f"{type(e).__name__}: {e}"
            win = latest_windows(winlog)
            native = []
            zombies = []
            for proc in Path("/proc").iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    if (proc / "comm").read_text().strip() == "mono_export":
                        state = (proc / "stat").read_text().split(")", 1)[1].split()[0]
                        (zombies if state == "Z" else native).append(int(proc.name))
                except (FileNotFoundError, ProcessLookupError):
                    pass
            row = {"utc": datetime.now(timezone.utc).isoformat(), "monotonic": time.monotonic(),
                   "guard_seconds": latency, "guard_error": error,
                   "statvfs": stat_values, "statvfs_error": stat_error,
                   "C_free_bytes": free_bytes, "native_pids": native,
                   "native_zombies": zombies,
                   "windows": win, "process_launch_audit_events": list(events)}
            out.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(json.dumps({"baseline_seconds": time.monotonic() - start, "samples": 61,
                      "output": str(args.output), "process_launch_audit_events": events}))


if __name__ == "__main__":
    main()
