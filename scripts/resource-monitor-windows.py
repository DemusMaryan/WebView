"""One Windows process, native APIs only; no per-sample subprocesses or WMI."""
import argparse
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


class FindData(c.Structure):
    _fields_ = [("attrs", w.DWORD), ("creation", w.FILETIME), ("access", w.FILETIME),
                ("write", w.FILETIME), ("high", w.DWORD), ("low", w.DWORD),
                ("r0", w.DWORD), ("r1", w.DWORD), ("name", w.WCHAR * 260), ("short", w.WCHAR * 14)]


class Performance(c.Structure):
    _fields_ = [("cb", w.DWORD)] + [(n, c.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        ("HandleCount", w.DWORD), ("ProcessCount", w.DWORD), ("ThreadCount", w.DWORD)]


class ProcessEntry(c.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", c.c_size_t),
                ("module", w.DWORD), ("threads", w.DWORD), ("ppid", w.DWORD), ("priority", w.LONG),
                ("flags", w.DWORD), ("name", w.WCHAR * 260)]


class ProcessMemory(c.Structure):
    _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [(n, c.c_size_t) for n in (
        "peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile",
        "peak_pagefile", "private")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vhdx", required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    if os.name != "nt" or not 0.5 <= args.interval <= 1:
        raise ValueError("Windows only; interval must be 0.5..1 seconds")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    psapi = c.WinDLL("psapi", use_last_error=True)
    kernel.FindFirstFileW.argtypes = [w.LPCWSTR, c.POINTER(FindData)]
    kernel.FindFirstFileW.restype = w.HANDLE
    kernel.FindClose.argtypes = [w.HANDLE]
    kernel.GetCompressedFileSizeW.argtypes = [w.LPCWSTR, c.POINTER(w.DWORD)]
    kernel.GetCompressedFileSizeW.restype = w.DWORD
    kernel.GetDiskFreeSpaceExW.argtypes = [w.LPCWSTR] + [c.POINTER(c.c_ulonglong)] * 3
    kernel.GetDiskFreeSpaceW.argtypes = [w.LPCWSTR] + [c.POINTER(w.DWORD)] * 4
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(ProcessMemory), w.DWORD]
    psapi.GetPerformanceInfo.argtypes = [c.POINTER(Performance), w.DWORD]
    invalid = c.c_void_p(-1).value
    paths = [r"C:\pagefile.sys", args.vhdx, r"C:\swapfile.sys"]
    paths += [str(p) for p in Path(os.environ["TEMP"]).glob("*/*swap*.vhdx")]
    paths += [str(p) for p in Path(os.environ["TEMP"]).glob("*swap*.vhdx")]
    watched = {"powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "wsl.exe",
               "wslhost.exe", "wslservice.exe"}

    def file_info(path):
        item = {"path": path, "logical_bytes": None, "allocated_bytes": None, "errors": {}}
        data = FindData()
        handle = kernel.FindFirstFileW(path, c.byref(data))
        if handle == invalid:
            item["errors"]["logical"] = c.get_last_error()
        else:
            item["logical_bytes"] = (data.high << 32) | data.low
            kernel.FindClose(handle)
        high = w.DWORD()
        c.set_last_error(0)
        low = kernel.GetCompressedFileSizeW(path, c.byref(high))
        error = c.get_last_error()
        if low == 0xffffffff and error:
            item["errors"]["allocated"] = error
        else:
            item["allocated_bytes"] = (high.value << 32) | low
        return item

    def memory(pid):
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            return {"rss_bytes": None, "private_bytes": None, "error": c.get_last_error()}
        try:
            info = ProcessMemory()
            info.cb = c.sizeof(info)
            if not psapi.GetProcessMemoryInfo(handle, c.byref(info), info.cb):
                return {"rss_bytes": None, "private_bytes": None, "error": c.get_last_error()}
            return {"rss_bytes": info.rss, "private_bytes": info.private, "error": None}
        finally:
            kernel.CloseHandle(handle)

    def processes():
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == invalid:
            return {"error": c.get_last_error(), "items": None, "counts": None}
        try:
            item = ProcessEntry()
            item.size = c.sizeof(item)
            rows = []
            ok = kernel.Process32FirstW(handle, c.byref(item))
            if not ok:
                return {"error": c.get_last_error(), "items": None, "counts": None,
                        "monitor_descendants": None}
            while ok:
                rows.append({"pid": item.pid, "ppid": item.ppid, "name": item.name.lower()})
                ok = kernel.Process32NextW(handle, c.byref(item))
            error = c.get_last_error()
            if error != 18:  # ERROR_NO_MORE_FILES is the normal end of enumeration.
                return {"error": error, "items": None, "counts": None,
                        "monitor_descendants": None}
        finally:
            kernel.CloseHandle(handle)
        family = {os.getpid()}
        for _ in range(len(rows)):
            next_family = family | {p["pid"] for p in rows if p["ppid"] in family}
            if next_family == family:
                break
            family = next_family
        selected = [p | memory(p["pid"]) for p in rows if p["name"] in watched or p["pid"] in family]
        return {"error": None, "items": selected,
                "counts": {name: sum(p["name"] == name for p in rows) for name in sorted(watched)},
                "monitor_descendants": sorted(family - {os.getpid()})}

    def sample():
        start = time.perf_counter()
        caller, total, free = c.c_ulonglong(), c.c_ulonglong(), c.c_ulonglong()
        disk = {"available_bytes": None, "total_free_bytes": None, "total_bytes": None, "error": None}
        if kernel.GetDiskFreeSpaceExW("C:\\", c.byref(caller), c.byref(total), c.byref(free)):
            disk.update(available_bytes=caller.value, total_free_bytes=free.value, total_bytes=total.value)
        else:
            disk["error"] = c.get_last_error()
        spc, bps, fc, tc = (w.DWORD() for _ in range(4))
        independent = {"free_bytes": None, "error": None}
        if kernel.GetDiskFreeSpaceW("C:\\", c.byref(spc), c.byref(bps), c.byref(fc), c.byref(tc)):
            independent["free_bytes"] = spc.value * bps.value * fc.value
        else:
            independent["error"] = c.get_last_error()
        perf = Performance()
        perf.cb = c.sizeof(perf)
        ram = {"available_bytes": None, "committed_bytes": None, "commit_limit_bytes": None, "error": None}
        if psapi.GetPerformanceInfo(c.byref(perf), perf.cb):
            ram.update(available_bytes=perf.PhysicalAvailable * perf.PageSize,
                       committed_bytes=perf.CommitTotal * perf.PageSize,
                       commit_limit_bytes=perf.CommitLimit * perf.PageSize)
        else:
            ram["error"] = c.get_last_error()
        return {"type": "sample", "utc": datetime.now(timezone.utc).isoformat(),
                "monotonic": start, "monitor_pid": os.getpid(), "C": disk,
                "C_cluster_api": independent, "memory": ram,
                "files": [file_info(p) for p in dict.fromkeys(paths)], "processes": processes(),
                "sample_seconds": time.perf_counter() - start, "monitor_cpu_seconds": time.process_time()}

    started = time.perf_counter()
    count = 0
    with args.output.open("x", encoding="utf-8", buffering=1) as out:
        out.write(json.dumps({"type": "meta", "pid": os.getpid(), "interval": args.interval,
                              "no_subprocess_calls": True, "drive": "C:", "vhdx": args.vhdx}) + "\n")
        while time.perf_counter() - started < args.seconds and not args.stop_file.exists():
            out.write(json.dumps(sample(), allow_nan=False, separators=(",", ":")) + "\n")
            out.flush()
            count += 1
            target = started + count * args.interval
            time.sleep(max(0, target - time.perf_counter()))
    print(json.dumps({"pid": os.getpid(), "samples": count, "cpu_seconds": time.process_time(),
                      "elapsed_seconds": time.perf_counter() - started, "output": str(args.output)}))


if __name__ == "__main__":
    main()
