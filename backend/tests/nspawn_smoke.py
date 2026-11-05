"""Explicit WSL-root smoke: real container app/health/stop, zero jobs or SLAM."""
import json
import os
import time
import subprocess
import threading
import httpx
from mtw import container_runtime as runtime
from mtw.autoscaler import health
from mtw.processes import identity
from mtw.config import DATA,CV_ROOT
from mtw.config import PORTS
from mtw.control import write_nginx
from mtw.service_auth import service_headers
from mtw.files import atomic_json


def main():
    runtime.require_root();runtime.prepare()
    orb=CV_ROOT/'src/ORB_SLAM3'
    libraries=':'.join(str(p) for p in (CV_ROOT/'prefix/lib',orb/'lib',orb/'Thirdparty/DBoW2/lib',orb/'Thirdparty/g2o/lib'))
    links=subprocess.run(runtime.command()[:-4]+['/usr/bin/env',f'LD_LIBRARY_PATH={libraries}',
        '/usr/bin/ldd',str(CV_ROOT/'build/exporter/mono_export')],capture_output=True,text=True,timeout=20)
    atomic_json(DATA/'reports/prompt11-container-linker.json',{'returncode':links.returncode,'stdout':links.stdout,'stderr':links.stderr})
    assert links.returncode==0 and 'not found' not in links.stdout,links.stdout+links.stderr
    process=runtime.launch();info=None;namespace=None
    proof={"creates_jobs":False,"passed":False,"native_linker_passed":True}
    startup_trace=[];done=threading.Event()
    def trace_startup():
        while not done.is_set():
            raw=runtime.candidate_launches()
            startup_trace.append({'at':time.time(),'raw':[{'pid':r['pid'],'ppid':runtime.process_parent(r['pid'])} for r in raw],
                'supervisors':[r['pid'] for r in runtime.outer_launches(raw,runtime.process_parent)]})
            done.wait(.01)
    tracer=threading.Thread(target=trace_startup,daemon=True);tracer.start()
    try:
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            info=identity(process.pid) or info
            namespace=runtime.namespace_of_supervisor(info) or namespace
            if process.poll() is not None: raise RuntimeError(f'nspawn exit {process.returncode}; inspect nspawn-compute-3.log')
            value=health('compute-3')
            if value:
                namespace=value['pid_namespace'];proof['health']=value
                assert namespace!=os.readlink('/proc/self/ns/pid')
                assert value['mount_namespace']!=os.readlink('/proc/self/ns/mnt')
                assert value['isolation']['root_read_only'] and value['isolation']['repository_read_only'] and value['isolation']['core_read_only']
                assert value['isolation']['jobs_writable'] and not value['isolation']['host_only_marker_visible']
                assert value['isolation']['uid']==1000 and value['isolation']['no_new_privileges']=='1'
                proof['processes']=runtime.namespace_members(namespace)
                write_nginx(add_server='compute-3')
                deadline_proxy=time.monotonic()+5
                while time.monotonic()<deadline_proxy:
                    response=httpx.get(f'http://127.0.0.1:{PORTS["balancer"]}/health',headers=service_headers(),timeout=1,trust_env=False)
                    if response.status_code==200 and response.json().get('server_id')=='compute-3':
                        proof['nginx_health']=response.json();break
                    time.sleep(.1)
                assert 'nginx_health' in proof,'Container not published through Nginx'
                break
            time.sleep(.1)
        else: raise TimeoutError('Container health timeout')
    finally:
        done.set();tracer.join(timeout=2)
        proof['startup_trace']=startup_trace
        try: write_nginx(exclude=('compute-3',))
        finally: runtime.stop(info,namespace,process)
        proof['remaining']=runtime.namespace_members(namespace)
        proof['supervisor_exit']=process.returncode
        proof['passed']='health' in proof and proof['remaining']==[]
        atomic_json(DATA/'reports/prompt11-nspawn-smoke.json',proof)
    print(json.dumps(proof))


if __name__=='__main__':main()
