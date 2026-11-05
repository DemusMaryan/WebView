"""Brief real container display probe, zero jobs; sync on browser observation."""
import argparse
from pathlib import Path
import time
from mtw import container_runtime as runtime
from mtw.autoscaler import health
from mtw.config import DATA
from mtw.files import atomic_json
from mtw.processes import identity


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--review',action='store_true');a=p.parse_args()
    prefix='container-review' if a.review else 'container'
    root=a.root.resolve();assert root.parent==(DATA/'reports').resolve() and root.name.startswith('admin-')
    runtime.require_root();runtime.prepare();process=runtime.launch();info=None;namespace=None
    proof={'creates_jobs':False,'passed':False}
    try:
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            info=identity(process.pid) or info;namespace=runtime.namespace_of_supervisor(info) or namespace
            if process.poll() is not None:raise RuntimeError('Container probe exited before health')
            value=health('compute-3')
            if value:
                proof['health']=value;namespace=value['pid_namespace'];break
            time.sleep(.1)
        else:raise TimeoutError('Container health deadline')
        proof['members']=runtime.namespace_members(namespace);atomic_json(root/f'{prefix}-probe.json',proof)
        deadline=time.monotonic()+35
        while not (root/f'{prefix}-viewed').exists() and time.monotonic()<deadline:time.sleep(.1)
        assert (root/f'{prefix}-viewed').exists(),'Browser did not observe container within budget'
        proof['passed']=True
    finally:
        runtime.stop(info,namespace,process)
        proof['remaining']=runtime.namespace_members(namespace);proof['supervisor_exit']=process.returncode
        atomic_json(root/f'{prefix}-probe.json',proof)
        (root/f'{prefix}-stopped').touch()
    print({'passed':proof['passed'],'remaining':proof['remaining']})
