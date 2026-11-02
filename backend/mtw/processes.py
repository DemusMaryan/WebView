"""Identify owned processes before signalling, including reparented native children."""
import asyncio
import ctypes
import os
from pathlib import Path
import signal


def identity(pid):
    try:
        path = Path(f"/proc/{pid}")
        stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        argv = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        # cmdline becomes empty during exit, sometimes after the first stat read.
        if not argv:
            return None
        return {"pid": int(pid), "start_ticks": stat[19], "argv": argv}
    except (FileNotFoundError, ProcessLookupError):
        return None


def subreaper():
    # Reap grandchildren if cancellation kills the Python worker first.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")


def members(pgid):
    result = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                if os.getpgid(int(path.name)) == pgid:
                    item = identity(int(path.name))
                    if item:
                        result.append(item)
            except ProcessLookupError:
                pass
    return result


async def terminate_group(pid, start_ticks, job_id, output):
    owned = identity(pid)
    if owned:
        if owned["start_ticks"] != start_ticks or "mtw.worker" not in owned["argv"] or str(job_id) not in owned["argv"]:
            raise RuntimeError("Worker PID ownership mismatch; refusing to signal")
    else:
        # A worker may already have exited; validate each remaining group member.
        for item in members(pid):
            if str(output) not in item["argv"] or "mono_export" not in item["argv"]:
                raise RuntimeError("Orphan process group ownership mismatch")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = asyncio.get_running_loop().time() + 2
    while members(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    if members(pid):
        os.killpg(pid, signal.SIGKILL)
    for _ in range(40):
        if not members(pid):
            break
        await asyncio.sleep(0.05)
    if members(pid):
        raise RuntimeError("Worker process group did not terminate")


def reap_group(pid):
    # Call after await child.wait(): asyncio owns the direct worker's wait status.
    while True:
        try:
            child, _ = os.waitpid(-pid, os.WNOHANG)
            if child == 0:
                break
        except ChildProcessError:
            break
