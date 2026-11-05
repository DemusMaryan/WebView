"""Project-local nspawn image and lifecycle; never changes host trust or settings."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from .config import CONFIG, CONFIG_PATH, CV_ROOT, DATA, REPO
from .processes import identity

ROOT = CV_ROOT / "containers/compute-3"
IMAGE = ROOT / "rootfs"
MACHINE = "mtw-compute-3"
CONTAINER_REPO = Path("/opt/monotrajectoryweb")
CONTAINER_CONFIG = Path("/etc/mtw/local.toml")
PYTHON = CV_ROOT / "web-venv/bin/python"


def require_root():
    if os.getuid() != 0: raise RuntimeError("nspawn lifecycle requires the explicit WSL root helper")


def prepare():
    require_root()
    from .capacity import ensure_space
    ensure_space()
    ROOT.mkdir(parents=True,exist_ok=True)
    for directory in ("usr","etc/mtw","etc/alternatives","proc","sys","dev","run","tmp","var","home",str(CONTAINER_REPO)[1:],
                      str(DATA)[1:],str(DATA/"jobs")[1:],str(DATA/"logs")[1:],str(DATA/"pids")[1:]):
        (IMAGE/directory).mkdir(parents=True,exist_ok=True)
    for name in ("bin","sbin","lib","lib64"):
        path=IMAGE/name
        if not path.is_symlink(): path.symlink_to("usr/"+name)
    (IMAGE/"etc/os-release").write_text('ID=ubuntu\nNAME="MonoTrajectoryWeb nspawn compute"\n')
    (IMAGE/"etc/passwd").write_text('root:x:0:0:root:/root:/bin/false\ndemus:x:1000:1000:compute:/tmp:/bin/false\n')
    (IMAGE/"etc/group").write_text('root:x:0:\ndemus:x:1000:\n')
    (IMAGE/"etc/nsswitch.conf").write_text('passwd: files\ngroup: files\nhosts: files dns\n')
    config = CONFIG_PATH.read_text()
    config = config.replace(f'cv_root = "{CONFIG["paths"]["cv_root"]}"',f'cv_root = "{CV_ROOT}"')
    config = config.replace(f'data_root = "{CONFIG["paths"]["data_root"]}"',f'data_root = "{DATA}"')
    config = config.replace(f'backing_mount = "{CONFIG["storage"]["backing_mount"]}"',f'backing_mount = "{CONTAINER_REPO}"')
    (ROOT/"container.toml").write_text(config)
    os.chmod(ROOT/"container.toml",0o600); os.chown(ROOT/"container.toml",1000,1000)
    for name in ("database-url","service-key"):
        (IMAGE/str(DATA/name)[1:]).touch(exist_ok=True)
    (IMAGE/str(CONTAINER_CONFIG)[1:]).touch(exist_ok=True)
    (IMAGE/'etc/ld.so.cache').touch(exist_ok=True)
    for directory in (CV_ROOT/"web-venv",CV_ROOT/"prefix",CV_ROOT/"build/exporter",CV_ROOT/"src/ORB_SLAM3"):
        (IMAGE/str(directory)[1:]).mkdir(parents=True,exist_ok=True)
    return {"rootfs":str(IMAGE),"usr":"host read-only","repository":"single read-only bind, no copy"}


def command():
    from sqlalchemy import select
    from .db import Job,Session
    from .config import TERMINAL
    argv = ["/usr/bin/systemd-nspawn",f"--directory={IMAGE}",f"--machine={MACHINE}","--register=no","--settings=no",
        "--read-only","--bind-ro=/usr:/usr","--bind-ro=/etc/alternatives:/etc/alternatives",
        "--bind-ro=/etc/ld.so.cache:/etc/ld.so.cache",f"--bind-ro={REPO}:{CONTAINER_REPO}",
        f"--bind-ro={ROOT/'container.toml'}:{CONTAINER_CONFIG}","--console=pipe","--as-pid2","--private-users=no",
        "--user=demus","--drop-capability=all","--no-new-privileges=yes","--kill-signal=SIGTERM",
        "--tmpfs=/tmp:mode=1777",
        "--link-journal=no","--timezone=off","--resolv-conf=off",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",f"--setenv=PYTHONPATH={CONTAINER_REPO}/backend",
        f"--setenv=MTW_CONFIG={CONTAINER_CONFIG}","--setenv=MTW_SERVER_ID=compute-3","--setenv=HOME=/tmp"]
    for path in (CV_ROOT/"web-venv",CV_ROOT/"prefix",CV_ROOT/"build/exporter",CV_ROOT/"src/ORB_SLAM3",DATA/"database-url",DATA/"service-key"):
        argv.append(f"--bind-ro={path}:{path}")
    argv.append(f"--bind={DATA/'jobs'}:{DATA/'jobs'}")
    # Explicitly protect every already completed job, including all saved 54.
    with Session() as s:
        for job_id in s.scalars(select(Job.id).where(Job.status.in_(TERMINAL)).order_by(Job.id)):
            path=DATA/"jobs"/str(job_id)
            if path.is_dir() and not path.is_symlink(): argv.append(f"--bind-ro={path}:{path}")
    return argv+[str(PYTHON),"-m","mtw.service","compute-3"]


def namespace_members(namespace):
    result=[]
    if not namespace: return result
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            if os.readlink(p/'ns/pid') != namespace: continue
            stat=(p/'stat').read_text().rsplit(')',1)[1].split()
            nspid=next(line.split()[1:] for line in (p/'status').read_text().splitlines() if line.startswith('NSpid:'))
            result.append({"host_pid":int(p.name),"container_pid":int(nspid[-1]),"state":stat[0],
                "cpu_ticks":int(stat[11])+int(stat[12]),"rss_bytes":int(stat[21])*os.sysconf('SC_PAGE_SIZE'),
                "start_ticks":stat[19],"argv":(p/'cmdline').read_bytes().replace(b'\0',b' ').decode('utf-8','replace').strip()})
        except (FileNotFoundError,ProcessLookupError,PermissionError,StopIteration): pass
    return result


def outer_launches(candidates, parent_of):
    """A pre-exec nspawn child inherits argv; it is not another supervisor."""
    ids={item['pid'] for item in candidates}
    result=[]
    for item in candidates:
        pid=item['pid'];seen={pid}
        while True:
            pid=parent_of(pid)
            if pid in ids: break
            if not pid or pid in seen:
                result.append(item);break
            seen.add(pid)
    return result


def process_parent(pid):
    try: return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
    except (FileNotFoundError,ProcessLookupError): return None


def candidate_launches():
    matches=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        item=identity(int(p.name))
        if item and item['argv'].startswith('/usr/bin/systemd-nspawn ') and f'--machine={MACHINE} ' in item['argv'] and f'--directory={IMAGE} ' in item['argv']:
            matches.append(item)
    return matches


def owned_launches():
    return outer_launches(candidate_launches(),process_parent)


def namespace_of_supervisor(info):
    if not info: return None
    try:
        for pid in Path(f'/proc/{info["pid"]}/task/{info["pid"]}/children').read_text().split():
            namespace=os.readlink(f'/proc/{pid}/ns/pid')
            if namespace != os.readlink('/proc/self/ns/pid'): return namespace
    except (FileNotFoundError,ProcessLookupError): pass
    return None


def supervisor_alive(info):
    if not info: return False
    item=identity(info['pid'])
    return bool(item and item['start_ticks']==info['start_ticks'] and item['argv'].startswith('/usr/bin/systemd-nspawn ')
        and f'--machine={MACHINE} ' in item['argv'] and f'--directory={IMAGE} ' in item['argv'])


def launch():
    require_root()
    if owned_launches(): raise RuntimeError('Project nspawn already exists; adopt or stop it before a new launch')
    with (DATA/'logs/nspawn-compute-3.log').open('ab') as log:
        os.chown(log.name,1000,1000)
        process=subprocess.Popen(command(),stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    return process


def stop(info, namespace=None, process=None):
    require_root()
    item=identity(info['pid']) if info else None
    if item:
        if item['start_ticks'] != info['start_ticks'] or item not in owned_launches():
            raise RuntimeError('nspawn PID ownership mismatch; refusing to signal')
        actual=namespace_of_supervisor(info)
        if namespace and actual and namespace!=actual:
            raise RuntimeError('Namespace does not belong to the owned nspawn supervisor')
        namespace=actual or namespace
    members_before=namespace_members(namespace)
    if members_before and (not item or not namespace_of_supervisor(info)):
        raise RuntimeError('Cannot prove ownership of a surviving namespace; refusing to signal')
    tracked={r['host_pid']:r['start_ticks'] for r in members_before}
    if item:
        payload=next((r for r in namespace_members(namespace) if '-m mtw.service compute-3' in r['argv']),None)
        if payload:
            current=identity(payload['host_pid'])
            if current and current['start_ticks']==payload['start_ticks'] and os.readlink(f'/proc/{payload["host_pid"]}/ns/pid')==namespace:
                os.kill(payload['host_pid'],signal.SIGTERM)
        else:
            os.kill(item['pid'],signal.SIGTERM)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        if process: process.poll()
        if not namespace_members(namespace) and not (info and identity(info['pid'])): break
        time.sleep(.1)
    remaining=namespace_members(namespace)
    if remaining:
        # Killing this verified namespace init kills its own descendants only.
        init=next((r for r in remaining if r['container_pid']==1),None)
        if not init or tracked.get(init['host_pid']) != init['start_ticks']:
            raise RuntimeError('Cannot identify original owned container init; slot retained')
        os.kill(init['host_pid'],signal.SIGKILL)
    if process:
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired: raise RuntimeError('nspawn did not reap its namespace; slot retained') from None
    deadline=time.monotonic()+5
    while namespace_members(namespace) and time.monotonic()<deadline: time.sleep(.1)
    if namespace_members(namespace): raise RuntimeError('Container still has kernel processes; slot retained')
    deadline=time.monotonic()+5
    while any(Path(f'/proc/{pid}').exists() for pid in tracked) and time.monotonic()<deadline:
        if process: process.poll()
        time.sleep(.05)
    if any(Path(f'/proc/{pid}').exists() for pid in tracked):
        raise RuntimeError('Container left a kernel PID, including zombie; slot retained')
    item=identity(info['pid']) if info else None
    if item:
        if item['start_ticks'] != info['start_ticks'] or item not in owned_launches():
            raise RuntimeError('Supervisor PID was reused during stop; refusing to signal')
        os.kill(item['pid'],signal.SIGTERM)
        deadline=time.monotonic()+3
        while identity(item['pid']) and time.monotonic()<deadline: time.sleep(.1)
        if identity(item['pid']): raise RuntimeError('Owned nspawn supervisor still running')
