"""Real delayed orphan exit: reap only the owned group, without SLAM."""
import asyncio
import os
from pathlib import Path
import signal
import sys
import unittest
from mtw.processes import group_processes, reap_group, subreaper


class ProcessReaping(unittest.IsolatedAsyncioTestCase):
    async def test_late_orphan_is_reaped_and_other_group_is_untouched(self):
        subreaper()
        read_fd, write_fd = os.pipe()
        helper = other = task = None
        try:
            other = await asyncio.create_subprocess_exec(sys.executable, "-c",
                "import sys; sys.stdin.buffer.read(1)", stdin=asyncio.subprocess.PIPE, start_new_session=True)
            source = """import os,sys
pid=os.fork()
if pid:
 print(pid,flush=True)
 os._exit(0)
os.close(1);os.close(2)
os.read(int(sys.argv[1]),1)
os._exit(0)
"""
            helper = await asyncio.create_subprocess_exec(sys.executable, "-c", source, str(read_fd),
                pass_fds=(read_fd,), stdout=asyncio.subprocess.PIPE, start_new_session=True)
            orphan = int(await helper.stdout.readline())
            await helper.wait()
            task = asyncio.create_task(reap_group(helper.pid))
            await asyncio.sleep(0)  # Let the first nonblocking wait run; orphan is held by a pipe.
            self.assertFalse(task.done(), "Reaper returned before the orphan exited")
            os.write(write_fd, b"x")
            await task
            self.assertFalse(Path(f"/proc/{orphan}").exists())
            self.assertFalse(group_processes(helper.pid))
            self.assertIsNone(other.returncode)
        finally:
            os.close(read_fd)
            os.close(write_fd)
            if other and other.returncode is None:
                other.stdin.close()
                await other.wait()
            if helper:
                if group_processes(helper.pid):
                    os.killpg(helper.pid, signal.SIGKILL)
                await helper.wait()
                await reap_group(helper.pid)
